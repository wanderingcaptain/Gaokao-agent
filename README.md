# AI 高考志愿填报系统

## 🚀 快速启动

### 1. 安装依赖
```bash
cd backend
pip install -r requirements.txt
```

### 2. 配置环境变量
```bash
cp .env.example .env
# 编辑 .env，填入你的 QWEN_API_KEY
```

### 3. 初始化数据库 & 写入测试数据
```bash
cd backend
python -m scripts.seed_test_data
```

### 4. 启动服务
```bash
cd backend
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 5. 访问前端
打开浏览器访问：http://localhost:8000

---

## 📁 目录结构

```
gaokaozhiyuan-agent/
├── backend/
│   ├── main.py                  # FastAPI 入口
│   ├── routers/
│   │   ├── profile.py           # 用户画像接口
│   │   ├── recommend.py         # 推荐接口（SSE 流式）
│   │   └── detail.py            # 详情动态 UI 接口
│   ├── services/
│   │   ├── rule_engine.py       # 同位次转换 + 硬性过滤
│   │   ├── llm_service.py       # Qwen API 封装
│   │   └── profile_service.py   # 画像读写
│   ├── database/
│   │   └── models.py            # SQLAlchemy ORM（3张表）
│   ├── scripts/
│   │   └── seed_test_data.py    # 测试数据种子
│   ├── profiles/                # 用户画像 JSON 存储
│   ├── .env.example
│   └── requirements.txt
└── frontend/
    ├── index.html               # 首页 + 多步骤表单
    └── result.html              # 冲稳保结果页 + 动态详情
```

## 🔑 API 文档
启动后访问：http://localhost:8000/docs

## 📊 测试数据覆盖
- **省份**：广东（物理类/历史类），浙江（物理类）
- **院校**：30所（含北大/清华/复旦/交大/浙大/中大/华工等985/211）
- **专业**：计算机、人工智能、电子信息、数学、数据科学
- **年份**：2023 ~ 2025（三年历史数据）
