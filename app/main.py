"""HTTP API 层：路由、依赖注入、错误映射。

错误约定：400 无效时序/参数，404 资源不存在，409 冲突（重叠/来源环/重复批次/重复撤销）。
所有写操作在单事务内完成，拒绝时回滚、保持原状态。
"""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import db, service
from .schemas import (
    CleaningCreate,
    DeclarationCreate,
    DeclarationPreviewCreate,
    LabelCreate,
    MaterialCreate,
    ProcessingCreate,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="过敏原溯源服务",
    version="1.2.0",
    description="共线生产食品批次的过敏原与待确认状态溯源：原料声明、加工登记、"
                "设备残留与清洗、来源路径查询、标签签发、声明更正/清洗撤销的"
                "预演与凭据提交，以及按游标增量拉取溯源变化事件。",
    lifespan=lifespan,
)


@app.exception_handler(service.NotFound)
async def not_found_handler(request: Request, exc: service.NotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(service.Conflict)
async def conflict_handler(request: Request, exc: service.Conflict):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(service.BadRequest)
async def bad_request_handler(request: Request, exc: service.BadRequest):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- 基础

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 原料批次

@app.post("/materials", status_code=201, tags=["原料"])
def create_material(data: MaterialCreate, conn=Depends(get_conn)):
    """登记原料批次及其过敏原声明（或待确认状态）。"""
    return service.create_material(conn, data)


@app.get("/materials", tags=["原料"])
def list_materials(conn=Depends(get_conn)):
    return service.list_materials(conn)


@app.get("/materials/{batch_id}", tags=["原料"])
def get_material(batch_id: str, conn=Depends(get_conn)):
    return service.get_material(conn, batch_id)


@app.post("/materials/{batch_id}/declarations", status_code=201, tags=["原料"])
def add_declaration(batch_id: str, data: DeclarationCreate, conn=Depends(get_conn)):
    """更正原料声明：追加新版本。未签发结论按最新版本实时重算；已签发标签不变。"""
    return service.add_declaration(conn, batch_id, data)


@app.post("/materials/{batch_id}/declarations/preview", status_code=200, tags=["预演"])
def preview_declaration(batch_id: str, data: DeclarationPreviewCreate,
                        conn=Depends(get_conn)):
    """声明更正预演（不落库）：返回确有差异的下游批次（过敏原/待确认/来源路径/依据）
    及其已签发标签相对变更前后实时结论的差异，并给出提交凭据。"""
    return service.preview_declaration(conn, batch_id, data)


# ---------------------------------------------------------------- 加工批次

@app.post("/processings", status_code=201, tags=["加工"])
def create_processing(data: ProcessingCreate, conn=Depends(get_conn)):
    """登记加工批次：设备、UTC 起止、投入/产出批次。

    校验：时序有效、投入已存在且加工开始前可用、来源不成环、同设备不重叠、
    产出批次号未被占用。任一不满足即拒绝并保持原状态。
    """
    return service.create_processing(conn, data)


@app.get("/processings", tags=["加工"])
def list_processings(conn=Depends(get_conn)):
    return service.list_processings(conn)


@app.get("/processings/{processing_id}", tags=["加工"])
def get_processing(processing_id: str, conn=Depends(get_conn)):
    return service.get_processing(conn, processing_id)


# ---------------------------------------------------------------- 清洗

@app.post("/cleanings", status_code=201, tags=["清洗"])
def create_cleaning(data: CleaningCreate, conn=Depends(get_conn)):
    """登记设备清洗。只有 verified=true 且未被撤销、且发生在两次加工之间的清洗才清除残留。"""
    return service.create_cleaning(conn, data)


@app.get("/cleanings", tags=["清洗"])
def list_cleanings(equipment_id: str | None = None, conn=Depends(get_conn)):
    return service.list_cleanings(conn, equipment_id)


@app.get("/cleanings/{cleaning_id}", tags=["清洗"])
def get_cleaning(cleaning_id: int, conn=Depends(get_conn)):
    return service.get_cleaning(conn, cleaning_id)


@app.post("/cleanings/{cleaning_id}/revoke", tags=["清洗"])
def revoke_cleaning(cleaning_id: int, conn=Depends(get_conn)):
    """撤销清洗验证。撤销后未签发结论实时重算（残留恢复）；已签发标签不变。"""
    return service.revoke_cleaning(conn, cleaning_id)


@app.post("/cleanings/{cleaning_id}/revoke/preview", status_code=200, tags=["预演"])
def preview_revoke_cleaning(cleaning_id: int, conn=Depends(get_conn)):
    """清洗撤销预演（不落库）：返回确有差异的下游批次（含设备残留传播）
    及其已签发标签相对变更前后实时结论的差异，并给出提交凭据。"""
    return service.preview_revoke_cleaning(conn, cleaning_id)


# ---------------------------------------------------------------- 预演提交

@app.post("/previews/{preview_id}/commit", tags=["预演"])
def commit_preview(preview_id: str, conn=Depends(get_conn)):
    """凭预演凭据原子应用变更：影响本次差异的记录已变化则 409（需重新预演），
    无关记录更新不使凭据失效。返回实际影响（与预演同口径）。"""
    return service.commit_preview(conn, preview_id)


# ---------------------------------------------------------------- 溯源查询

@app.get("/batches/{batch_id}/trace", tags=["溯源"])
def trace_batch(batch_id: str, conn=Depends(get_conn)):
    """实时溯源：返回每项过敏原与待确认状态的来源路径及当前有效依据。"""
    return service.trace_response(conn, batch_id)


# ---------------------------------------------------------------- 标签

@app.post("/batches/{batch_id}/labels", status_code=201, tags=["标签"])
def issue_label(batch_id: str, data: LabelCreate, conn=Depends(get_conn)):
    """签发标签：冻结签发时刻的结论与依据，后续变更不影响本标签。
    可通过 supersedes_id 关联被替代的旧版标签。"""
    return service.issue_label(conn, batch_id, data.supersedes_id)


@app.get("/batches/{batch_id}/labels", tags=["标签"])
def list_labels(batch_id: str, conn=Depends(get_conn)):
    return service.list_labels(conn, batch_id)


@app.get("/labels/{label_id}", tags=["标签"])
def get_label(label_id: int, conn=Depends(get_conn)):
    return service.get_label(conn, label_id)


# ---------------------------------------------------------------- 增量变化事件

@app.get("/changes/snapshot", tags=["增量变化"])
def get_changes_snapshot(conn=Depends(get_conn)):
    """初始化读取：返回同一状态下现存批次的实时结论（含来源路径）与依据，
    以及当前高水位游标。随后携带该游标调用 /changes/events 增量补事件。"""
    return service.changes_snapshot(conn)


@app.get("/changes/events", tags=["增量变化"])
def get_change_events(cursor: str | None = Query(
                          None, description="上一次返回的不透明游标；"
                                            "首次拉取可传快照接口返回的游标"),
                      limit: int = Query(
                          100, ge=1, le=1000, description="单页事件数（1-1000）"),
                      conn=Depends(get_conn)):
    """按游标分页读取溯源变化事件。首页将上界固定为读取开始时的高水位，
    后续页沿用该上界，期间的新写入不会进入本次读取；caught_up=true 后
    返回的游标不再带上界，下一次拉取自动固定新上界。无效游标返回 400。"""
    return service.list_change_events(conn, cursor, limit)
