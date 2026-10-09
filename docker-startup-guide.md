# SWE 服务 Docker 本地启动指南

## 环境要求

- Windows 10/11 + Docker Desktop（Linux 容器模式，linux/amd64 平台）
- 已启用 WSL2 后端

## 快速启动

如果 Docker 镜像 `copaw-swe:local` 已存在且数据已持久化到宿主机，直接启动容器：

```bash
# 启动已存在的容器
docker start copaw-swe

# 或如果容器已删除，重新创建（保留数据挂载）
docker run -d --name copaw-swe \
  --restart unless-stopped \
  -e SWE_ENV=dev \
  -e SWE_DB_HOST=120.48.112.239 \
  -e SWE_DB_USER=mysqladmin \
  -e SWE_DB_PASSWORD=123456 \
  -e SWE_DB_NAME=rmassistdata \
  -e SWE_DB_ACCESS=BEE_123456 \
  -e MARKET_API_BASE_URL=http://host.docker.internal:8090/api \
  -e SWE_ALLOW_MISSING_TRACE_SDK=true \
  -e SWE_RUNNING_IN_CONTAINER=1 \
  -e SWE_PORT=8088 \
  -p 8088:8088 \
  -v C:/Users/dengquanbo/.swe/working:/app/working \
  -v C:/Users/dengquanbo/.swe/working.secret:/app/working.secret \
  copaw-swe:local
```

服务地址：http://localhost:8088

---

## 完整步骤

### 第一步：构建 Docker 镜像

```bash
cd D:\workspace\CoPaw

# 构建本地开发镜像
docker build -f deploy/Dockerfile.local -t copaw-swe:local .
```

> 注意：首次构建需要安装 Python 3.12（通过 pyenv 从源码编译），耗时约 10-15 分钟。

### 第二步：准备宿主机数据目录

```bash
# 创建数据持久化目录
mkdir -p C:/Users/dengquanbo/.swe/working
mkdir -p C:/Users/dengquanbo/.swe/working.secret
```

> 如需迁移已有数据，先从旧容器复制：
> ```bash
> docker cp old_container:/app/working/. C:/Users/dengquanbo/.swe/working/
> docker cp old_container:/app/working.secret/. C:/Users/dengquanbo/.swe/working.secret/
> ```

### 第三步：启动容器

```bash
docker run -d --name copaw-swe \
  --restart unless-stopped \
  -e SWE_ENV=dev \
  -e SWE_DB_HOST=120.48.112.239 \
  -e SWE_DB_USER=mysqladmin \
  -e SWE_DB_PASSWORD=123456 \
  -e SWE_DB_NAME=rmassistdata \
  -e SWE_DB_ACCESS=BEE_123456 \
  -e MARKET_API_BASE_URL=http://host.docker.internal:8090/api \
  -e SWE_ALLOW_MISSING_TRACE_SDK=true \
  -e SWE_RUNNING_IN_CONTAINER=1 \
  -e SWE_PORT=8088 \
  -p 8088:8088 \
  -v C:/Users/dengquanbo/.swe/working:/app/working \
  -v C:/Users/dengquanbo/.swe/working.secret:/app/working.secret \
  copaw-swe:local
```

### 第四步：验证服务

```bash
# 检查容器状态
docker ps --filter "name=copaw-swe"

# 检查应用日志
docker logs --tail 50 copaw-swe

# 测试源配置端点（需要正确租户头）
curl -s -H "X-Tenant-Id: ODAyODAxOTU.Uk1BU1NJU1Q" -H "X-Source-Id: RMASSIST" \
  http://localhost:8088/api/source-system-config/effective

# 测试认证状态端点
curl -s -H "X-Tenant-Id: ODAyODAxOTU.Uk1BU1NJU1Q" -H "X-Source-Id: RMASSIST" \
  http://localhost:8088/api/auth/status
```

---

## 环境变量说明

| 变量名 | 说明 | 示例值 |
|--------|------|--------|
| `SWE_ENV` | 运行环境 | `dev` |
| `SWE_DB_HOST` | MySQL 主机 | `120.48.112.239` |
| `SWE_DB_PORT` | MySQL 端口 | `3306` |
| `SWE_DB_USER` | MySQL 用户 | `mysqladmin` |
| `SWE_DB_PASSWORD` | MySQL 密码 | `123456` |
| `SWE_DB_NAME` | MySQL 数据库名 | `rmassistdata` |
| `SWE_DB_ACCESS` | 访问密钥 | `BEE_123456` |
| `MARKET_API_BASE_URL` | Market 服务地址 | `http://host.docker.internal:8090/api` |
| `SWE_ALLOW_MISSING_TRACE_SDK` | 跳过 trace SDK 检查 | `true` |
| `SWE_RUNNING_IN_CONTAINER` | 标记为容器运行 | `1` |
| `SWE_PORT` | 服务监听端口 | `8088` |
| `SWE_SECRET_DIR` | 密钥目录（容器内） | `/app/working.secret` |
| `SWE_WORKING_DIR` | 工作目录（容器内） | `/app/working` |

---

## 数据持久化

容器数据通过 Docker volume 挂载到宿主机：

| 宿主机路径 | 容器内路径 | 说明 |
|-----------|-----------|------|
| `C:\Users\dengquanbo\.swe\working` | `/app/working` | 工作区数据、租户配置、源模板 |
| `C:\Users\dengquanbo\.swe\working.secret` | `/app/working.secret` | 密钥、Provider 配置 |

删除容器不会丢失数据，重新创建时挂载相同路径即可。

---

## 日志查看

### Docker Desktop GUI
容器列表 → 选择 `copaw-swe` → `Logs` 标签页

### 命令行

```bash
# 实时跟踪日志
docker logs -f copaw-swe

# 最近 100 行
docker logs --tail 100 copaw-swe

# 过滤错误
docker logs copaw-swe 2>&1 | grep -i error

# 应用错误日志（supervisord 管理）
docker exec copaw-swe tail -f /var/log/app.err.log

# 应用标准输出
docker exec copaw-swe tail -f /var/log/app.out.log
```

### 容器内日志文件

| 文件路径 | 内容 |
|----------|------|
| `/var/log/app.err.log` | 应用错误日志 |
| `/var/log/app.out.log` | 应用标准输出 |
| `/var/log/xvfb.err.log` | Xvfb X 服务器错误 |
| `/var/log/xfce4.err.log` | XFCE4 桌面环境错误 |
| `/var/log/supervisord.log` | 进程管理日志 |
| `/var/log/dbus.err.log` | D-Bus 系统日志 |

---

## 常用运维命令

```bash
# 启动
docker start copaw-swe

# 停止
docker stop copaw-swe

# 重启
docker restart copaw-swe

# 进入容器 shell
docker exec -it copaw-swe bash

# 查看运行进程
docker exec copaw-swe supervisorctl status

# 容器内安装依赖（临时，镜像重建会丢失）
docker exec copaw-swe pip install <package>

# 查看容器信息
docker inspect copaw-swe

# 查看资源使用
docker stats copaw-swe

# 删除容器（数据不丢失）
docker rm -f copaw-swe
```

---

## 故障排查

### 容器启动后立即退出

```bash
# 查看退出原因
docker logs copaw-swe

# 检查 supervisord 状态
docker exec copaw-swe supervisorctl status
```

### 端口已被占用

```bash
# 检查 8088 端口占用
netstat -ano | findstr :8088

# 更换端口映射 -p 8089:8088
```

### 数据库连接失败

- 确认宿主机 MySQL 可访问：`mysql -h 120.48.112.239 -u mysqladmin -p`
- 检查 `SWE_DB_*` 环境变量是否正确
- 确认 MySQL 允许 `mysqladmin@'%'` 连接：`SHOW GRANTS FOR 'mysqladmin'@'%';`

### Tenant bootstrap unavailable

这是因为源模板 `default_RMASSIST` 目录缺失引导文件。检查并创建：

```bash
# 检查是否存在
docker exec copaw-swe ls /app/working/default_RMASSIST/

# 如果缺失，从 default 复制并修复路径
docker exec copaw-swe cp -r /app/working/default /app/working/default_RMASSIST
```

然后在容器内修复 `config.json` 和 `agent.json` 中的 `workspace_dir` 路径，将 `/app/working/default` 替换为 `/app/working/default_RMASSIST`。

### 应用循环重启（exit status 3）

通常是数据库连接问题或缺少依赖，检查日志：
```bash
docker logs --tail 100 copaw-swe 2>&1 | grep -i "error\|exception\|failed"
```

---

## 目录结构

```
C:\Users\dengquanbo\.swe\
└── working\
    ├── config.json              # 全局配置
    ├── default\                 # 全局默认源模板
    │   ├── config.json
    │   ├── workspaces/
    │   │   └── default/        # 默认工作区
    │   │       ├── AGENTS.md
    │   │       ├── HEARTBEAT.md
    │   │       ├── MEMORY.md
    │   │       ├── PROFILE.md
    │   │       ├── SOUL.md
    │   │       ├── agent.json
    │   │       ├── chats.json
    │   │       ├── jobs.json
    │   │       ├── token_usage.json
    │   │       ├── memory/
    │   │       ├── sessions/
    │   │       └── skills/
    │   ├── media/
    │   ├── secrets/
    │   └── skill_pool/
    ├── default_RMASSIST\        # RMASSIST 源模板（由 default 复制并修改路径）
    ├── default_test\            # 测试用源模板
    ├── workspaces\
    │   └── default\            # 租户工作区模板
    └── skill_pool\             # 技能池

C:\Users\dengquanbo\.swe\
└── working.secret\
    └── default\
        └── providers\          # 默认 Provider 配置
```

---

## 重新构建镜像

代码更新后需要重新构建：

```bash
cd D:\workspace\CoPaw

# 重新构建（使用缓存）
docker build -f deploy/Dockerfile.local -t copaw-swe:local .

# 无缓存重建（修改了依赖时）
docker build -f deploy/Dockerfile.local --no-cache -t copaw-swe:local .
```

重建后需重新创建容器：

```bash
docker stop copaw-swe && docker rm copaw-swe
# 然后重新执行上述启动命令
```
