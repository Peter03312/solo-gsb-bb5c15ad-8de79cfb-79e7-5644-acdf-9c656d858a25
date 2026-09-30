"""端到端测试：登记校验、残留继承、清洗与撤销、声明更正、标签冻结。"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# 每个测试会话使用独立数据库文件
_tmp = tempfile.mkdtemp(prefix="allergen-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    # 重置数据库
    db.init_db()
    conn = db.connect()
    for t in ["material_declarations", "material_batches", "processing_inputs",
              "processing_outputs", "processings", "cleanings", "labels",
              "change_events"]:
        conn.execute(f"DELETE FROM {t}")
    # 重置自增序号：测试内事件从 seq=1 开始（生产库不删表，序号持续递增）
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('change_events', 'labels')")
    conn.close()
    with TestClient(app) as c:
        yield c


def material(c, batch_id, allergens=None, pending=False, declared_at="2026-01-01T00:00:00Z"):
    return c.post("/materials", json={
        "batch_id": batch_id, "allergens": allergens or [],
        "pending": pending, "declared_at": declared_at})


def processing(c, pid, eq, start, end, inputs, outputs):
    return c.post("/processings", json={
        "processing_id": pid, "equipment_id": eq,
        "start_utc": start, "end_utc": end,
        "inputs": inputs, "outputs": outputs})


# ---------------------------------------------------------------- 基础登记与校验

def test_material_and_trace(client):
    r = material(client, "RM-1", allergens=["milk", "peanut"])
    assert r.status_code == 201, r.text
    t = client.get("/batches/RM-1/trace").json()
    assert t["allergens"] == ["milk", "peanut"]
    assert t["pending"] is False
    assert t["allergen_paths"]["milk"][0][0]["via"] == "declaration"


def test_pending_material(client):
    material(client, "RM-P", pending=True)
    t = client.get("/batches/RM-P/trace").json()
    assert t["pending"] is True
    assert t["pending_paths"][0][0]["batch_id"] == "RM-P"


def test_duplicate_material_rejected(client):
    assert material(client, "RM-1").status_code == 201
    r = material(client, "RM-1")
    assert r.status_code == 409


def test_invalid_time_range_rejected(client):
    material(client, "RM-1")
    r = processing(client, "P1", "EQ-1",
                   "2026-01-02T10:00:00Z", "2026-01-02T09:00:00Z",
                   ["RM-1"], ["OUT-1"])
    assert r.status_code == 400
    assert "无效时序" in r.json()["detail"]
    # 原状态保持：无加工记录
    assert client.get("/processings").json() == []


def test_naive_datetime_rejected(client):
    material(client, "RM-1")
    r = processing(client, "P1", "EQ-1",
                   "2026-01-02T08:00:00", "2026-01-02T09:00:00",
                   ["RM-1"], ["OUT-1"])
    assert r.status_code == 422  # 必须带时区


def test_unknown_input_rejected(client):
    r = processing(client, "P1", "EQ-1",
                   "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                   ["GHOST"], ["OUT-1"])
    assert r.status_code == 400


def test_input_not_available_before_start(client):
    # 原料 1 月 2 日 10 点才登记，加工 9 点开始 -> 不可用
    material(client, "RM-LATE", declared_at="2026-01-02T10:00:00Z")
    r = processing(client, "P1", "EQ-1",
                   "2026-01-02T09:00:00Z", "2026-01-02T11:00:00Z",
                   ["RM-LATE"], ["OUT-1"])
    assert r.status_code == 400
    assert "不可用" in r.json()["detail"]
    # 上游产出未结束就投入也不可用
    material(client, "RM-2", declared_at="2026-01-01T00:00:00Z")
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T11:00:00Z", "2026-01-02T12:00:00Z",
                      ["RM-2"], ["MID-1"]).status_code == 201
    r = processing(client, "P3", "EQ-2",
                   "2026-01-02T11:30:00Z", "2026-01-02T12:30:00Z",
                   ["MID-1"], ["OUT-3"])
    assert r.status_code == 400  # MID-1 12:00 才可用


def test_equipment_overlap_rejected(client):
    material(client, "RM-1")
    material(client, "RM-2")
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T10:00:00Z",
                      ["RM-1"], ["OUT-1"]).status_code == 201
    # 部分重叠
    r = processing(client, "P2", "EQ-1",
                   "2026-01-02T09:00:00Z", "2026-01-02T11:00:00Z",
                   ["RM-2"], ["OUT-2"])
    assert r.status_code == 409
    assert "重叠" in r.json()["detail"]
    # 相邻不重叠（前序结束即开始）允许
    assert processing(client, "P3", "EQ-1",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["RM-2"], ["OUT-3"]).status_code == 201
    # 不同设备同时间段允许
    assert processing(client, "P4", "EQ-2",
                      "2026-01-02T08:30:00Z", "2026-01-02T09:30:00Z",
                      ["RM-2"], ["OUT-4"]).status_code == 201


def test_cycle_and_output_conflict_rejected(client):
    material(client, "RM-1")
    # 自环：同一批次既是投入又是产出
    r = processing(client, "P1", "EQ-1",
                   "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                   ["RM-1"], ["RM-1"])
    assert r.status_code == 409
    assert "成环" in r.json()["detail"]
    # 产出批次号已被占用
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-1"], ["OUT-1"]).status_code == 201
    r = processing(client, "P3", "EQ-1",
                   "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                   ["RM-1"], ["OUT-1"])
    assert r.status_code == 409
    assert "冲突" in r.json()["detail"]
    # 把已登记的原料当产出也是来源冲突
    r = processing(client, "P4", "EQ-1",
                   "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                   ["OUT-1"], ["RM-1"])
    assert r.status_code == 409


# ---------------------------------------------------------------- 继承与残留

def _build_line(client):
    """EQ-1: P1(RM-MILK -> OUT-1) 08-09, P2(RM-OAT -> OUT-2) 10-11"""
    material(client, "RM-MILK", allergens=["milk"])
    material(client, "RM-OAT", allergens=["gluten"])
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-MILK"], ["OUT-1"]).status_code == 201
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["RM-OAT"], ["OUT-2"]).status_code == 201


def test_residue_inherited_without_cleaning(client):
    _build_line(client)
    t = client.get("/batches/OUT-2/trace").json()
    # 投入继承 gluten + 设备残留 milk
    assert t["allergens"] == ["gluten", "milk"]
    residue_paths = [p for p in t["allergen_paths"]["milk"]
                     if p[-1]["via"] == "equipment_residue"]
    assert residue_paths, "milk 应来自设备残留"
    p = residue_paths[0]
    assert [s.get("batch_id") for s in p] == ["RM-MILK", "OUT-1", "OUT-2"]
    assert p[-1]["equipment_id"] == "EQ-1"


def test_verified_cleaning_clears_residue(client):
    _build_line(client)
    r = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True})
    assert r.status_code == 201
    t = client.get("/batches/OUT-2/trace").json()
    assert t["allergens"] == ["gluten"]
    assert t["basis"]["cleanings"][0]["id"] == r.json()["id"]


def test_unverified_cleaning_does_not_clear(client):
    _build_line(client)
    client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": False})
    t = client.get("/batches/OUT-2/trace").json()
    assert "milk" in t["allergens"]


def test_revoked_cleaning_restores_residue(client):
    _build_line(client)
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    assert client.get("/batches/OUT-2/trace").json()["allergens"] == ["gluten"]
    # 撤销清洗 -> 未签发结论按当前有效记录重算，残留恢复
    r = client.post(f"/cleanings/{cid}/revoke")
    assert r.status_code == 200
    assert r.json()["revoked"] is True
    t = client.get("/batches/OUT-2/trace").json()
    assert "milk" in t["allergens"]
    # 重复撤销拒绝
    assert client.post(f"/cleanings/{cid}/revoke").status_code == 409


def test_cleaning_outside_window_does_not_clear(client):
    _build_line(client)
    # 清洗发生在 P2 开始之后，不在两次加工之间
    client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T10:30:00Z",
        "verified": True})
    t = client.get("/batches/OUT-2/trace").json()
    assert "milk" in t["allergens"]


def test_pending_propagates_through_processing_and_residue(client):
    material(client, "RM-PEND", pending=True)
    material(client, "RM-OK", allergens=["soy"])
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-PEND"], ["OUT-1"]).status_code == 201
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["RM-OK"], ["OUT-2"]).status_code == 201
    t = client.get("/batches/OUT-2/trace").json()
    assert t["pending"] is True  # 残留带入待确认
    assert t["pending_paths"][0][-1]["via"] == "equipment_residue"


# ---------------------------------------------------------------- 声明更正与标签

def test_declaration_correction_recomputes_live(client):
    _build_line(client)
    # 更正：RM-OAT 实际含 sesame
    r = client.post("/materials/RM-OAT/declarations",
                    json={"allergens": ["gluten", "sesame"], "pending": False})
    assert r.status_code == 201
    assert r.json()["version"] == 2
    t = client.get("/batches/OUT-2/trace").json()
    assert t["allergens"] == ["gluten", "milk", "sesame"]
    # 依据记录为第 2 版声明
    versions = {d["batch_id"]: d["version"]
                for d in t["basis"]["material_declarations"]}
    assert versions["RM-OAT"] == 2


def test_label_frozen_and_superseded(client):
    _build_line(client)
    # 签发标签
    r = client.post("/batches/OUT-2/labels", json={})
    assert r.status_code == 201
    label1 = r.json()
    assert label1["conclusion"]["allergens"] == ["gluten", "milk"]
    # 声明更正 + 清洗撤销等后续变更
    client.post("/materials/RM-OAT/declarations",
                json={"allergens": ["gluten", "sesame"]})
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    # 旧标签不变
    old = client.get(f"/labels/{label1['label_id']}").json()
    assert old["conclusion"]["allergens"] == ["gluten", "milk"]
    # 实时结论已变（清洗生效，milk 消失；sesame 出现）
    live = client.get("/batches/OUT-2/trace").json()
    assert live["allergens"] == ["gluten", "sesame"]
    # 签发关联旧版的新标签
    r = client.post("/batches/OUT-2/labels",
                    json={"supersedes_id": label1["label_id"]})
    assert r.status_code == 201
    label2 = r.json()
    assert label2["supersedes_id"] == label1["label_id"]
    assert label2["conclusion"]["allergens"] == ["gluten", "sesame"]
    # 撤销清洗后，两张已签发标签仍不变
    client.post(f"/cleanings/{cid}/revoke")
    assert client.get(f"/labels/{label2['label_id']}").json()[
        "conclusion"]["allergens"] == ["gluten", "sesame"]
    # 新实时结论再次包含 milk
    assert "milk" in client.get("/batches/OUT-2/trace").json()["allergens"]
    # 标签历史
    labels = client.get("/batches/OUT-2/labels").json()
    assert [l["label_id"] for l in labels] == [label1["label_id"], label2["label_id"]]


def test_label_supersedes_validation(client):
    _build_line(client)
    material(client, "RM-X")
    l1 = client.post("/batches/OUT-2/labels", json={}).json()
    # 旧标签属于别的批次 -> 拒绝
    r = client.post("/batches/RM-X/labels",
                    json={"supersedes_id": l1["label_id"]})
    assert r.status_code == 400
    # 旧标签不存在
    assert client.post("/batches/RM-X/labels",
                       json={"supersedes_id": 999}).status_code == 404


def test_unknown_batch_404(client):
    assert client.get("/batches/NOPE/trace").status_code == 404
    assert client.post("/batches/NOPE/labels", json={}).status_code == 404


# ---------------------------------------------------------------- 预演与凭据提交

def test_declaration_preview_is_dry_run_and_reports_diffs(client):
    _build_line(client)  # EQ-1: P1(RM-MILK->OUT-1), P2(RM-OAT->OUT-2)，无清洗
    # 预演前签发标签：冻结 gluten + milk
    label = client.post("/batches/OUT-2/labels", json={}).json()
    r = client.post("/materials/RM-OAT/declarations/preview",
                    json={"allergens": ["gluten", "sesame"]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["dry_run"] is True and d["applied"] is False
    assert d["change"]["type"] == "declaration_correction"
    assert d["change"]["new_version"] == 2
    token = d["preview_id"]
    assert token
    affected = {b["batch_id"]: b for b in d["affected_batches"]}
    assert "OUT-2" in affected          # 下游批次（设备残留链外的 OUT-1 不受影响）
    assert "OUT-1" not in affected
    out2 = affected["OUT-2"]
    assert out2["before"]["conclusion"]["allergens"] == ["gluten", "milk"]
    assert out2["after"]["conclusion"]["allergens"] == ["gluten", "milk", "sesame"]
    assert out2["diff"]["conclusion"]["allergens"]["added"] == ["sesame"]
    basis_diff = out2["diff"]["basis"]["material_declarations"]["RM-OAT"]
    assert basis_diff["before"]["version"] == 1
    assert basis_diff["after"]["version"] == 2
    # 已签发标签：与变更前实时结论一致（null），与变更后结论存在差异
    labs = out2["issued_labels"]
    assert len(labs) == 1 and labs[0]["label_id"] == label["label_id"]
    assert labs[0]["diff_vs_before"] is None
    assert labs[0]["diff_vs_after"]["allergens"]["added"] == ["sesame"]
    # 预演不落库：声明仍为 v1，实时结论无 sesame，标签接口兼容
    assert client.get("/materials/RM-OAT").json()["version"] == 1
    assert "sesame" not in client.get("/batches/OUT-2/trace").json()["allergens"]
    return token


def test_declaration_preview_commit_applies_atomically(client):
    token = test_declaration_preview_is_dry_run_and_reports_diffs(client)
    label_id = client.get("/batches/OUT-2/labels").json()[0]["label_id"]
    r = client.post(f"/previews/{token}/commit")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["dry_run"] is False and d["applied"] is True
    assert d["affected_count"] >= 1
    assert client.get("/materials/RM-OAT").json()["version"] == 2
    live = client.get("/batches/OUT-2/trace").json()
    assert live["allergens"] == ["gluten", "milk", "sesame"]
    # 已签发标签不改写；凭据一次性
    assert client.get(f"/labels/{label_id}").json()[
        "conclusion"]["allergens"] == ["gluten", "milk"]
    assert client.post(f"/previews/{token}/commit").status_code == 409


def test_preview_credential_invalidated_by_related_change(client):
    _build_line(client)
    token = client.post("/materials/RM-OAT/declarations/preview",
                        json={"allergens": ["sesame"]}).json()["preview_id"]
    # 影响差异的记录变化：目标原料又追加了声明版本
    client.post("/materials/RM-OAT/declarations", json={"allergens": ["gluten"]})
    r = client.post(f"/previews/{token}/commit")
    assert r.status_code == 409
    assert "重新预演" in r.json()["detail"]
    # 落库未发生：仍是直接更正写入的内容
    assert client.get("/materials/RM-OAT").json()["allergens"] == ["gluten"]


def test_preview_credential_survives_unrelated_records(client):
    _build_line(client)
    token = client.post("/materials/RM-OAT/declarations/preview",
                        json={"allergens": ["gluten", "sesame"]}).json()["preview_id"]
    # 无关原料、无关设备的加工登记不应使凭据失效
    material(client, "RM-X", allergens=["soy"])
    assert processing(client, "PX", "EQ-X",
                      "2026-02-02T08:00:00Z", "2026-02-02T09:00:00Z",
                      ["RM-X"], ["OUT-X"]).status_code == 201
    r = client.post(f"/previews/{token}/commit")
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True
    assert client.get("/materials/RM-OAT").json()["version"] == 2


def test_preview_credential_invalidated_by_new_related_processing(client):
    _build_line(client)
    token = client.post("/materials/RM-MILK/declarations/preview",
                        json={"allergens": []}).json()["preview_id"]
    # 预演后新增消费 RM-MILK 的加工 -> 实际影响面扩大，凭据必须失效
    assert processing(client, "P3", "EQ-2",
                      "2026-01-03T08:00:00Z", "2026-01-03T09:00:00Z",
                      ["RM-MILK"], ["OUT-3"]).status_code == 201
    assert client.post(f"/previews/{token}/commit").status_code == 409


def test_preview_credential_invalidated_by_new_label(client):
    _build_line(client)
    token = client.post("/materials/RM-OAT/declarations/preview",
                        json={"allergens": ["sesame"]}).json()["preview_id"]
    # 受影响批次上新签发标签 -> 标签差异集合变化，凭据失效
    assert client.post("/batches/OUT-2/labels", json={}).status_code == 201
    assert client.post(f"/previews/{token}/commit").status_code == 409


def test_revoke_preview_reports_residue_propagation_and_commit(client):
    _build_line(client)
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    # 清洗生效时签发标签（仅 gluten）
    label_id = client.post("/batches/OUT-2/labels", json={}).json()["label_id"]
    # 再加第三道加工：撤销清洗后残留沿 OUT-1 -> OUT-2 -> OUT-3 继续传播
    assert processing(client, "P3", "EQ-1",
                      "2026-01-02T12:00:00Z", "2026-01-02T13:00:00Z",
                      ["RM-OAT"], ["OUT-3"]).status_code == 201
    r = client.post(f"/cleanings/{cid}/revoke/preview")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["change"]["type"] == "cleaning_revocation"
    affected = {b["batch_id"]: b for b in d["affected_batches"]}
    assert set(affected) == {"OUT-2", "OUT-3"}
    out3 = affected["OUT-3"]
    assert "milk" in out3["after"]["conclusion"]["allergens"]
    milk_paths = out3["after"]["conclusion"]["allergen_paths"]["milk"]
    vias = [s["via"] for s in milk_paths[0]]
    assert vias == ["declaration", "input", "equipment_residue", "equipment_residue"]
    # 依据变化：before 依赖清洗 cid，after 无清洗依据
    assert str(cid) in affected["OUT-2"]["diff"]["basis"]["cleanings"]
    # OUT-2 标签与变更前一致、与变更后相差 milk（设备残留）
    labs = affected["OUT-2"]["issued_labels"]
    assert labs[0]["diff_vs_before"] is None
    assert labs[0]["diff_vs_after"]["allergens"]["added"] == ["milk"]
    # 预演期间清洗未撤销
    assert client.get(f"/cleanings/{cid}").json()["revoked"] is False
    # 提交：原子应用，实际影响与预演一致
    commit = client.post(f"/previews/{d['preview_id']}/commit")
    assert commit.status_code == 200, commit.text
    assert commit.json()["applied"] is True
    assert client.get(f"/cleanings/{cid}").json()["revoked"] is True
    assert "milk" in client.get("/batches/OUT-3/trace").json()["allergens"]
    assert client.get(f"/labels/{label_id}").json()[
        "conclusion"]["allergens"] == ["gluten"]
    # 既有直接撤销接口语义不变：重复撤销 409
    assert client.post(f"/cleanings/{cid}/revoke").status_code == 409


def test_revoke_preview_survives_unrelated_cleaning(client):
    _build_line(client)
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    token = client.post(f"/cleanings/{cid}/revoke/preview").json()["preview_id"]
    # 无关设备的未验证清洗不影响任何残留结论
    client.post("/cleanings", json={
        "equipment_id": "EQ-9", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": False})
    assert client.post(f"/previews/{token}/commit").status_code == 200


def test_revoke_preview_empty_diff_guards_upstream_declarations(client):
    # P1 的投入原料无过敏原：撤销清洗当下无差异；预演后若上游补报过敏原，
    # 撤销将变得有影响 —— 凭据必须失效。
    material(client, "RM-BLANK")
    material(client, "RM-OAT", allergens=["gluten"])
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-BLANK"], ["OUT-1"]).status_code == 201
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["RM-OAT"], ["OUT-2"]).status_code == 201
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    prev = client.post(f"/cleanings/{cid}/revoke/preview").json()
    # 结论（过敏原/待确认/路径）无差异，但依据（清洗）有差异 -> 仍列出
    assert prev["affected_count"] == 1
    entry = prev["affected_batches"][0]
    assert entry["batch_id"] == "OUT-2"
    assert entry["diff"]["conclusion"] is None
    assert str(cid) in entry["diff"]["basis"]["cleanings"]
    token = prev["preview_id"]
    # 上游补报 milk -> 撤销将把 milk 残留带进 OUT-2（结论开始受影响）
    client.post("/materials/RM-BLANK/declarations", json={"allergens": ["milk"]})
    assert client.post(f"/previews/{token}/commit").status_code == 409
    # 重新预演后可见结论差异并可提交
    prev2 = client.post(f"/cleanings/{cid}/revoke/preview").json()
    entry2 = [b for b in prev2["affected_batches"] if b["batch_id"] == "OUT-2"][0]
    assert entry2["diff"]["conclusion"]["allergens"]["added"] == ["milk"]
    assert client.post(f"/previews/{prev2['preview_id']}/commit").status_code == 200
    assert "milk" in client.get("/batches/OUT-2/trace").json()["allergens"]


def test_preview_invalid_targets_and_errors(client):
    _build_line(client)
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    # 无效目标沿用现有错误语义
    assert client.post("/materials/NOPE/declarations/preview",
                       json={"allergens": ["x"]}).status_code == 404
    assert client.post("/materials/OUT-1/declarations/preview",
                       json={"allergens": ["x"]}).status_code == 404  # 产出批次非原料
    assert client.post("/cleanings/999/revoke/preview").status_code == 404
    client.post(f"/cleanings/{cid}/revoke")
    assert client.post(f"/cleanings/{cid}/revoke/preview").status_code == 409  # 重复撤销
    assert client.post("/previews/bogus-token/commit").status_code == 409
    # 请求格式错误仍为 422（时间/字段校验保持一致）
    r = client.post("/materials/RM-OAT/declarations/preview", json={"allergens": "x"})
    assert r.status_code == 422


# ---------------------------------------------------------------- 增量变化事件

def _decode_cursor(token):
    import base64
    import json
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


def test_snapshot_initial_read_is_consistent(client):
    # 空库：快照为空、高水位 0
    s = client.get("/changes/snapshot").json()
    assert s["batch_count"] == 0 and s["as_of_seq"] == 0
    assert _decode_cursor(s["cursor"]) == {"v": 1, "a": 0}
    _build_line(client)  # 2 原料 + 2 加工产出 = 4 个现存批次
    s = client.get("/changes/snapshot").json()
    assert s["batch_count"] == 4
    assert {b["batch_id"] for b in s["batches"]} == {
        "RM-MILK", "RM-OAT", "OUT-1", "OUT-2"}
    out2 = [b for b in s["batches"] if b["batch_id"] == "OUT-2"][0]
    assert out2["conclusion"]["allergens"] == ["gluten", "milk"]
    assert s["as_of_seq"] == _decode_cursor(s["cursor"])["a"]
    # 快照游标只应看到新事件（本快照之后无事件 -> 空页且追上）
    r = client.get(f"/changes/events?cursor={s['cursor']}")
    assert r.status_code == 200
    d = r.json()
    assert d["events"] == [] and d["caught_up"] is True
    assert d["as_of_seq"] == s["as_of_seq"] == d["bounded_at_seq"]


def test_new_batch_events_have_null_before(client):
    r = material(client, "RM-1", allergens=["milk"])
    assert r.status_code == 201
    d = client.get("/changes/events").json()
    assert len(d["events"]) == 1
    e = d["events"][0]
    assert e["seq"] == 1
    assert e["op"] == "material_registered" and e["via"] == "direct"
    assert e["batch_id"] == "RM-1" and e["kind"] == "material"
    assert e["before"] is None  # 新批次前态为空
    assert e["after"]["conclusion"]["allergens"] == ["milk"]
    assert e["op_detail"]["batch_id"] == "RM-1"


def test_processing_events_ordered_by_batch_id_with_residue_change(client):
    material(client, "RM-MILK", allergens=["milk"])
    material(client, "RM-OAT", allergens=["gluten"])
    # 同设备先有 P0 -> 再登记 P1（OUT-2 经残留获得 milk）
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-MILK"], ["ZZZ-1"]).status_code == 201
    d = client.get("/changes/events").json()
    assert [e["batch_id"] for e in d["events"]] == ["RM-MILK", "RM-OAT", "ZZZ-1"]
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["RM-OAT"], ["AAA-2"]).status_code == 201
    # 同次操作仅新批次一个事件（既有批次 ZZZ-1 的实时溯源未变），按批次号
    after = client.get(
        "/changes/events", params={"cursor": d["next_cursor"]}).json()
    assert [e["batch_id"] for e in after["events"]] == ["AAA-2"]
    e = after["events"][0]
    assert e["op"] == "processing_registered"
    assert e["op_detail"]["processing_id"] == "P2"
    assert e["after"]["conclusion"]["allergens"] == ["gluten", "milk"]


def test_verified_cleaning_event_for_affected_batch_only(client):
    _build_line(client)
    r = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True})
    assert r.status_code == 201
    cid = r.json()["id"]
    d = client.get("/changes/events", params={"limit": 100}).json()
    evs = [e for e in d["events"] if e["op"] == "cleaning_registered"]
    assert [e["batch_id"] for e in evs] == ["OUT-2"]  # 仅确有变化的下游批次
    e = evs[0]
    assert e["before"]["conclusion"]["allergens"] == ["gluten", "milk"]
    assert e["after"]["conclusion"]["allergens"] == ["gluten"]
    assert e["op_detail"] == {
        "cleaning_id": cid, "equipment_id": "EQ-1",
        "cleaned_at": "2026-01-02T09:30:00Z", "verified": True}


def test_unverified_cleaning_produces_no_events(client):
    _build_line(client)
    before = client.get("/changes/events", params={"limit": 100}).json()
    hwm = before["as_of_seq"]
    r = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": False})
    assert r.status_code == 201
    after = client.get("/changes/events").json()
    assert after["as_of_seq"] == hwm  # 未验证清洗未改变实时溯源 -> 无事件


def test_label_issue_produces_no_events(client):
    _build_line(client)
    s = client.get("/changes/snapshot").json()
    assert client.post("/batches/OUT-2/labels", json={}).status_code == 201
    assert client.post("/batches/OUT-2/labels", json={}).status_code == 201
    d = client.get(f"/changes/events?cursor={s['cursor']}").json()
    assert d["events"] == []  # 标签签发不改变实时溯源，旧标签始终不改


def test_direct_correction_events_carry_full_views(client):
    _build_line(client)
    r = client.post("/materials/RM-OAT/declarations",
                    json={"allergens": ["gluten", "sesame"]})
    assert r.status_code == 201
    d = client.get("/changes/events", params={"limit": 100}).json()
    evs = [e for e in d["events"] if e["op"] == "declaration_corrected"]
    # 同次操作按批次号排序：目标原料本身（依据版本变化）+ 下游 OUT-2
    assert [e["batch_id"] for e in evs] == ["OUT-2", "RM-OAT"]
    by_batch = {e["batch_id"]: e for e in evs}
    e = by_batch["OUT-2"]
    assert e["via"] == "direct"
    assert e["before"]["conclusion"]["allergens"] == ["gluten", "milk"]
    assert e["after"]["conclusion"]["allergens"] == ["gluten", "milk", "sesame"]
    # 依据完整：命中声明版本 v1 -> v2
    versions = {x["batch_id"]: x["version"]
                for x in e["after"]["basis"]["material_declarations"]}
    assert versions["RM-OAT"] == 2
    rm = by_batch["RM-OAT"]
    assert rm["before"]["conclusion"]["allergens"] == ["gluten"]
    assert rm["after"]["conclusion"]["allergens"] == ["gluten", "sesame"]


def test_direct_revocation_event_restores_residue(client):
    _build_line(client)
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    r = client.post(f"/cleanings/{cid}/revoke")
    assert r.status_code == 200
    d = client.get("/changes/events", params={"limit": 100}).json()
    evs = [e for e in d["events"] if e["op"] == "cleaning_revoked"]
    assert [e["batch_id"] for e in evs] == ["OUT-2"]
    e = evs[0]
    assert e["before"]["conclusion"]["allergens"] == ["gluten"]
    assert e["after"]["conclusion"]["allergens"] == ["gluten", "milk"]
    assert e["op_detail"]["cleaning_id"] == cid


def test_preview_commit_emits_events_preview_does_not(client):
    _build_line(client)
    # 预演不落库、不产事件
    prev = client.post("/materials/RM-OAT/declarations/preview",
                       json={"allergens": ["sesame"]}).json()
    s = client.get("/changes/snapshot").json()
    assert s["as_of_seq"] == _decode_cursor(s["cursor"])["a"]
    # 提交成功 -> 与正式更正同口径事件，但 via=preview_commit
    r = client.post(f"/previews/{prev['preview_id']}/commit")
    assert r.status_code == 200
    d = client.get(f"/changes/events?cursor={s['cursor']}").json()
    assert [e["batch_id"] for e in d["events"]] == ["OUT-2", "RM-OAT"]
    e = [x for x in d["events"] if x["batch_id"] == "OUT-2"][0]
    assert e["op"] == "declaration_corrected" and e["via"] == "preview_commit"
    assert e["op_detail"]["preview_id"] == prev["preview_id"]
    assert e["after"]["conclusion"]["allergens"] == ["milk", "sesame"]


def test_failed_write_emits_no_events(client):
    material(client, "RM-1")
    hwm0 = client.get("/changes/events").json()["as_of_seq"]
    # 无效加工（投入不存在）整体回滚：无业务写入也无事件
    r = processing(client, "P-BAD", "EQ-1",
                   "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                   ["GHOST"], ["OUT-BAD"])
    assert r.status_code == 400
    assert client.get("/changes/events").json()["as_of_seq"] == hwm0
    # 重复登记原料冲突：无事件
    assert material(client, "RM-1").status_code == 409
    assert client.get("/changes/events").json()["as_of_seq"] == hwm0


def test_pagination_pins_fixed_upper_bound(client):
    material(client, "RM-1", allergens=["milk"])
    material(client, "RM-0", allergens=["egg"])  # 共 2 个事件，单页 1 条
    first = client.get("/changes/events", params={"limit": 1}).json()
    assert len(first["events"]) == 1 and first["events"][0]["seq"] == 1
    bound = first["bounded_at_seq"]
    assert bound == 2 and first["caught_up"] is False
    cur = _decode_cursor(first["next_cursor"])
    assert cur == {"v": 1, "a": 1, "b": 2}  # 翻页游标固定了上界
    # 读取进行中发生新写入
    material(client, "RM-2", allergens=["soy"])
    page2 = client.get("/changes/events",
                       params={"cursor": first["next_cursor"], "limit": 1}).json()
    # 固定上界内的最后一条；新写入（seq=3）不得泄漏
    assert [e["seq"] for e in page2["events"]] == [2]
    assert page2["caught_up"] is True
    assert page2["bounded_at_seq"] == bound
    nxt = _decode_cursor(page2["next_cursor"])
    assert nxt == {"v": 1, "a": 2}  # 追上后游标不再带上界
    # 下一轮自动固定新上界，可见 seq=3
    page3 = client.get("/changes/events",
                       params={"cursor": page2["next_cursor"]}).json()
    assert [e["seq"] for e in page3["events"]] == [3]
    assert page3["bounded_at_seq"] == 3 and page3["caught_up"] is True


def test_invalid_cursors_rejected(client):
    material(client, "RM-1", allergens=["milk"])
    for bad in ["!!!not-base64!!!", "bm90anNvbg",
                "eyJhIjogMX0"]:  # 分别为：非 base64 / 非 JSON / 缺版本字段
        r = client.get("/changes/events", params={"cursor": bad})
        assert r.status_code == 400, bad
    # after 超过当前高水位 -> 拒绝
    r = client.get("/changes/events", params={"cursor": _make_cursor(999, None)})
    assert r.status_code == 400
    # after > bound -> 拒绝
    r = client.get("/changes/events", params={"cursor": _make_cursor(5, 3)})
    assert r.status_code == 400
    # 固定上界超过当前高水位 -> 拒绝
    r = client.get("/changes/events", params={"cursor": _make_cursor(0, 999)})
    assert r.status_code == 400


def _make_cursor(after, bound):
    import base64
    import json
    body = {"v": 1, "a": after}
    if bound is not None:
        body["b"] = bound
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_snapshot_plus_events_reconstructs_current_state(client):
    material(client, "RM-1", allergens=["milk"])
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-1"], ["OUT-1"]).status_code == 201
    # 初始化读取
    s = client.get("/changes/snapshot").json()
    state = {b["batch_id"]: b["conclusion"] for b in s["batches"]}
    # 之后发生：更正 + 新加工
    client.post("/materials/RM-1/declarations",
                json={"allergens": ["milk", "egg"]})
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["RM-1"], ["OUT-2"]).status_code == 201
    # 按游标补完全部事件
    cursor = s["cursor"]
    while True:
        d = client.get("/changes/events",
                       params={"cursor": cursor, "limit": 1}).json()
        for e in d["events"]:
            if e["before"] is None:
                state[e["batch_id"]] = e["after"]["conclusion"]
            else:
                state[e["batch_id"]] = e["after"]["conclusion"]
        cursor = d["next_cursor"]
        if d["caught_up"]:
            break
    # 重建结果与最终实时结论一致
    live1 = client.get("/batches/OUT-1/trace").json()
    live2 = client.get("/batches/OUT-2/trace").json()
    assert state["OUT-1"]["allergens"] == live1["allergens"] == ["egg", "milk"]
    assert state["OUT-2"]["allergens"] == live2["allergens"] == ["egg", "milk"]
