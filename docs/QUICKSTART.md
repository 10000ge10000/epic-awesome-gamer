# 🚀 快速开始

## 一键部署（仅需 1 条命令）

```bash
# 设置 API Key 并一键部署
export API_KEY="sk-你的Key"
curl -fsSL https://raw.githubusercontent.com/10000ge10000/epic-kiosk/main/install.sh | bash
```

脚本会自动完成：
- ✅ 检测系统架构
- ✅ 克隆项目代码
- ✅ 配置 API Key
- ✅ 本地编译镜像
- ✅ 启动服务

---

## 手动部署（3 步）

### 0️⃣ 准备 API Key

访问 [SiliconFlow 官网](https://cloud.siliconflow.cn/i/OVI2n57p) 注册并获取 API Key。

> 🆓 **为什么选择 SiliconFlow？**
> - 主力模型 **Qwen2.5-7B-Instruct 完全免费**
> - 验证码模型价格极低（¥0.5/百万 tokens）
> - 国内访问速度快，无需科学上网
> - 本项目专为该平台优化
>
> 👉 **使用邀请链接注册，双方各得 ¥16 代金券**

### 1️⃣ 克隆项目
```bash
git clone https://github.com/10000ge10000/epic-kiosk.git
cd epic-kiosk
```

### 2️⃣ 配置 API Key
编辑 `docker-compose.yml`，找到 `API_KEY` 配置项：
```yaml
- API_KEY=sk-你的Key  # 替换为你的 SiliconFlow API Key
```

### 3️⃣ 构建并启动
```bash
docker compose up -d --build
```

> ⏱️ 首次构建约需 5-10 分钟（下载依赖 + 编译镜像）

---

## ✅ 访问控制台

打开浏览器：`http://服务器IP:18000`

在 Web 界面添加 Epic 账号，系统会自动处理后续所有流程。

---

## 📝 说明

- **无需配置文件**：所有配置都在 `docker-compose.yml` 中
- **无需配置账号**：Epic 账号在 Web 界面添加
- **模型已优化**：双模型自动切换，无需手动配置
- **主力模型免费**：Qwen2.5-7B-Instruct 完全免费
- **专属优化**：针对 SiliconFlow 平台优化

---

## 🔍 查看日志

```bash
# 查看 Worker 日志
docker logs epic-worker -f

# 查看 Web 日志
docker logs epic-web -f

# 查看所有服务状态
docker compose ps
```

---

## 🛑 停止服务

```bash
docker compose down
```

---

## 🔄 更新项目

```bash
cd epic-kiosk
git pull
docker compose up -d --build
```

---

更多详细信息请查看 [README.md](../README.md)
