# 把数据放到阿里云

项目数据分两类：**数据库**（SQLite `data/football.db`）和 **文件**（`data/*.json`、`data/reports/` 等）。可按下面几种方式在阿里云上使用。

---

## 一、数据库放到阿里云 RDS（推荐先做）

把比赛、预测等存到云上数据库，本机或 ECS 都连同一个库。

### 1. 在阿里云创建数据库

- 登录 [阿里云 RDS](https://rdsnext.console.aliyun.com/)
- 创建实例：选 **MySQL 8.0** 或 **PostgreSQL**，按量付费即可
- 创建数据库、设置白名单（放行你本机/ECS 的 IP）、记下 **内网地址、端口、库名、账号、密码**

### 2. 本地配置连接

在项目根目录 `.env` 里设置（不要提交到 Git）：

```bash
# MySQL 示例（把 host、port、库名、用户、密码换成你的）
DATABASE_URL=mysql+pymysql://用户名:密码@rm-xxxx.mysql.rds.aliyuncs.com:3306/数据库名

# PostgreSQL 示例
# DATABASE_URL=postgresql://用户名:密码@pg-xxxx.pg.rds.aliyuncs.com:5432/数据库名
```

### 3. 安装驱动并初始化表结构

```bash
# MySQL
pip install pymysql

# 若用 PostgreSQL
# pip install psycopg2-binary

# 建表（会按 schema 创建表，不覆盖已有数据）
python main.py ingest --max-matches 0
# 或先跑一条会触发建表的命令，例如 list / report
python main.py list
```

### 4. 迁移已有 SQLite 数据（可选）

若本地已有 `data/football.db` 且想迁到 RDS：

- 用工具导出 SQLite 再导入 MySQL/PostgreSQL（表结构需一致），或
- 在 RDS 建好表后，用脚本从 SQLite 读表并写入 RDS（可写一次性迁移脚本）。

迁移完成后，本机/ECS 都只连 `DATABASE_URL`，不再用本地 `football.db`。

---

## 二、整份应用（代码 + 数据）放到 ECS

在阿里云 ECS 上跑代码，数据落在 ECS 磁盘的 `data/` 目录。

### 1. 准备 ECS

- 创建 ECS（如 Ubuntu 22.04），绑定公网 IP 或通过内网访问
- 安全组放行：SSH 22、若对外提供 Web 再放行 8000 等

### 2. 部署代码

```bash
# 在 ECS 上
git clone <你的仓库> football_anlyision   # 或 scp/rsync 上传项目
cd football_anlyision
pip install -r requirements.txt
```

### 3. 配置环境变量

在 ECS 项目根目录建 `.env`，填写：

- `DATABASE_URL`（若用 RDS，填 RDS 内网地址；若用本机 SQLite 可先不设）
- `API_FOOTBALL_KEY`、`FOOTBALL_DATA_API_KEY` 等
- `LLM_*`（若在 ECS 上生成报告）

数据目录用默认即可：`data/` 在项目下，无需改。

### 4. 可选：Web 服务

若要在 ECS 上提供 Web：

```bash
# 安装 uvicorn（若 requirements 里没有）
pip install uvicorn
uvicorn api.app:app --host 0.0.0.0 --port 8000
# 或用 supervisor/systemd 常驻
```

---

## 三、把 data/ 备份到对象存储 OSS

不改变程序逻辑，只把 `data/` 定期备份到阿里云 OSS，防止丢数据。

### 1. 开通 OSS、创建 Bucket

- 在 [阿里云 OSS 控制台](https://oss.console.aliyun.com/) 创建 Bucket（选地域、私有/公共读按需）
- 创建 AccessKey（RAM 用户即可），记下 AccessKeyId、AccessKeySecret

### 2. 用 ossutil 或脚本同步

**方式 A：ossutil（推荐）**

```bash
# 安装 ossutil：见 https://help.aliyun.com/document_detail/120075.html
ossutil cp -r data/ oss://你的bucket/football_anlyision/data/ --update
```

可按需定期执行，实现备份。

**方式 B：Python 脚本 + oss2**

```bash
pip install oss2
```

脚本里遍历 `data/` 下文件，用 `oss2.Bucket.put_object_from_file()` 上传；或先打 tar 再上传一个备份文件。

### 3. 恢复

需要时从 OSS 把对应前缀（如 `football_anlyision/data/`）下载回 ECS 或本机的 `data/` 目录即可。

---

## 四、组合建议

| 需求           | 做法 |
|----------------|------|
| 只把数据库放云上 | 用 **一、RDS**，本机/ECS 都设 `DATABASE_URL` |
| 在云上跑应用     | 用 **二、ECS**，代码和数据都在 ECS，数据库可接 RDS |
| 怕丢 data 文件  | 用 **三、OSS** 定期备份 `data/` |
