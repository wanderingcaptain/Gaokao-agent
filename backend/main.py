"""
FastAPI 应用入口
启动命令：uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""
import os
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from dotenv import load_dotenv

from database.models import init_db
from routers import auth, profile, recommend, detail, agent, multi_agent

load_dotenv()

# ── 应用初始化 ────────────────────────────────
app = FastAPI(
    title="AI 高考志愿填报系统",
    version="1.0.0",
    description="结合规则引擎与大模型的智能志愿推荐服务",
)

# ── CORS（前端本地文件直接访问 API）────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 路由注册 ──────────────────────────────────
app.include_router(profile.router)
app.include_router(recommend.router)
app.include_router(detail.router)
app.include_router(auth.router)
app.include_router(agent.router)
app.include_router(multi_agent.router)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if request.url.path in {"/api/v1/auth/login", "/api/v1/auth/register"}:
        errors = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
                  for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})
    return await request_validation_exception_handler(request, exc)


# ── 静态文件（前端）──────────────────────────
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.exists(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/form")
    async def read_index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    @app.get("/result")
    async def read_result():
        return FileResponse(os.path.join(FRONTEND_DIR, "result.html"), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    @app.get("/")
    @app.get("/chat")
    async def read_chat():
        return FileResponse(os.path.join(FRONTEND_DIR, "chat.html"), headers={"Cache-Control": "no-cache"})


# ── 启动时建表 ────────────────────────────────
@app.on_event("startup")
async def startup_event():
    init_db()
    agent.recover_interrupted()
    multi_agent.ensure_worker()
    print("🚀 AI 高考志愿填报系统已启动")
    print(f"   文档地址：http://localhost:{os.getenv('PORT', 8000)}/docs")


@app.on_event("shutdown")
async def shutdown_event():
    await multi_agent.shutdown_worker()


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "gaokao-ai"}
