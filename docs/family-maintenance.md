# 家庭版部署与维护

此分支在官方发布版上增加独立运维入口。应用继续使用现有 Web/API、数据源和认证；
自有维护代码集中在 `scripts/family_*.py`、`docker/family-compose.yml`。
本文面向维护者，未同步成英文文档，因为这些功能用于私人中文家庭部署。

## 目录与版本

源码与运行目录分开，运行目录不进入 Git：

```text
~/Services/family-stock/
  source/                         # 自己的 fork，family/main 分支
  deployment/                     # --root 指向这里
    family-ops.json                # 独立端口、项目名、主机 UID/GID
    environments/production/
      deployment.json             # 固定 sha256 镜像 ID、源码提交、回退快照
      compose.yml
      data/runtime.env             # 唯一业务配置来源，可在网页保存 API Key
      data/.admin_password_hash    # 密码哈希
      data/.session_secret         # 会话密钥
      logs/ reports/ strategies/
    environments/staging/         # 独立数据库、配置、端口和会话密钥
    backups/ builds/ recoveries/ operations/ maintenance/
```

`data`、报告和自定义策略在主机持久化；镜像只运行固定版本。
自定义策略挂载到 `/app/custom-strategies`，与镜像内官方策略叠加，不覆盖官方目录。
运行目录及备份可能包含账户记录和 Key，保持私人权限，不上传 GitHub。
备份在同一台 Mac 上；防范磁盘损坏还需另外复制到用户自己的备份介质。

Mac 的 Docker 运行时可由 Docker Desktop 或 Colima 提供。以下示例假设 `docker`、
`git` 已可运行。若使用独立的 `runtime-stock`，先加载它的 `env.sh`。
Python 3.9 及以上即可运行维护脚本，无需安装应用 Python 依赖。

## 初次部署

```bash
cd ~/Services/family-stock/source
source ~/LocalAI/runtime-stock/env.sh
export DOCKER_CONTEXT=colima-stock
python3 scripts/family_ops.py --root ../deployment init
python3 scripts/family_ops.py --root ../deployment build --ref HEAD
# 用上一条输出的不可变 image ID，不能填写 latest
python3 scripts/family_ops.py --root ../deployment deploy --image sha256:<image-id>
python3 scripts/family_ops.py --root ../deployment status
```

初次部署关闭定时分析、启动立即分析、Bot、新闻自动采集和公开搜索实例；不放示例 Key。
只有网页和无需模型的管理功能可验收。首次密码尚未设置时 `public_ready=false`，
只允许本机或可信的私人入口访问。登录并设置初始密码后，再配置外部入口。
正式版默认绑定 `127.0.0.1:8000`，测试版绑定 `127.0.0.1:8001`；可在 `init` 参数中调整。
不要把两个实例挂在同一 hostname 的不同路径下；现有前端使用根路径 API。

在网页设置页填写模型与搜索配置即可保存到 `data/runtime.env`。
Compose 不整体注入另一份 `.env`，避免旧启动值覆盖网页保存的 Key。
定时任务由维护脚本控制，网页开关与部署控制应保持一致：

```bash
python3 scripts/family_ops.py --root ../deployment schedule --enable
python3 scripts/family_ops.py --root ../deployment schedule --disable
```

没有已配置的模型 Key 时启用操作会被拒绝。此检查只确认填写状态，
首次仍需用一只熟悉的股票完成真实模型请求，核对数据日期与指标口径。
分析日程使用容器 `Asia/Shanghai` 时区；主机备份日程使用 Mac 本地时区。

## 备份、测试升级和恢复

```bash
python3 scripts/family_ops.py --root ../deployment backup --retention-days 14
python3 scripts/family_ops.py --root ../deployment check-updates
```

备份先停止同一 Compose 项目的写入服务，包含隐藏认证文件和有效配置，校验 SHA256，
完成后恢复此前运行的服务。维护命令共用互斥锁，自动拉起任务不会越过正在进行的备份。
保留最近 14 天及当前回退所需备份；日志由 Docker 自身限制大小。

官方升级先检查发布说明，再在干净的开发分支合入选定版本。
`check-updates` 只获取标签及记录结果，不合并、不发布，也不执行 reset 或覆盖代码。
有冲突时逐项处理；自行修改的页面、策略或字段仍需验证。

```bash
# 在干净的代码分支合并并验证官方更新后，构建已提交的候选版本
python3 scripts/family_ops.py --root ../deployment build --ref <candidate-commit>
python3 scripts/family_ops.py --root ../deployment stage --image sha256:<candidate-image-id>
```

测试版从停止写入后的生产快照复制数据，关闭调度、Bot/监听及通知凭据，并更换自己的会话密钥。
测试数据库和生产数据库不同；在测试版修改配置和持仓不会修改正式数据。
检查登录、持仓、自选、历史报告、自定义策略及手机页面；涉及数据算法时另测价格复权及指标样本。

```bash
python3 scripts/family_ops.py --root ../deployment promote
python3 scripts/family_ops.py --root ../deployment rollback
# 或恢复指定快照；恢复前自动保留当前状态
python3 scripts/family_ops.py --root ../deployment restore <backup-directory-name>
```

发布只提升测试镜像，保留正式版配置与数据；发布前自动备份。
数据库初始化、健康和认证检查失败时恢复旧镜像与对应快照。
历史备份恢复会回到其时间点，期间新增记录保留在恢复前快照/恢复目录，必要时人工核对后再导回。
不要手动清理仍需回滚的本地镜像；备份记录不等同于镜像导出。

## Mac 自动维护

```bash
python3 scripts/family_mac.py --root ../deployment install \
  --runtime-bin ~/LocalAI/runtime-stock/bin \
  --docker-config ~/LocalAI/runtime-stock/docker-config \
  --docker-context colima-stock
```

安装后生成 `deployment/stockctl`，已选择正确 Docker context 和工具路径，可直接运行：

```bash
~/Services/family-stock/deployment/stockctl status
~/Services/family-stock/deployment/stockctl backup
~/Services/family-stock/deployment/stockctl check-updates
```

用户级 LaunchAgent 在用户登录后运行，不是未登录时的系统服务：

- 每 2 分钟恢复已部署的固定镜像，不自动换版本。
- 每 15 分钟记录健康与认证状态；缺模型配置单独显示为待配置。
- 每天 Mac 本地时间 01:45 备份，保留 14 天。
- 每周日 Mac 本地时间 09:00 检查官方标签。

记录位于 `deployment/maintenance/health.json` 和 `last-*.json`，只保存最新结果，
不保存 Key 值或完整配置。`operations/update-check.json` 保留更新检查结果。
这些任务不会通过聊天、邮件或企业微信发送通知；以后可按实际需要接入自己的通知渠道。
运行时本身也需要登录后自启，并让 Mac 在预期使用时保持运行。

## 本地验证

```bash
python3 -m py_compile scripts/family_ops.py scripts/family_mac.py
python3 -m unittest discover -s tests -p test_family_ops.py -v
python3 -m pytest tests/test_docker_entrypoint.py
```

自动检查覆盖维护失败路径。真实部署另外核对：认证未登录为 401、登录后管理 API、
重建后的数据和密码持久化、备份恢复、测试隔离及失败候选回滚。
缺少模型和搜索 Key 时，不将网页健康检查当作完整 AI 分析可用的证明。
