# 🔑 SiliconFlow API Key 获取指南

## 为什么选择 SiliconFlow？

本项目**推荐使用** [SiliconFlow](https://cloud.siliconflow.cn/i/OVI2n57p) 的 API Key：

> 🎁 **使用邀请链接注册：[https://cloud.siliconflow.cn/i/OVI2n57p](https://cloud.siliconflow.cn/i/OVI2n57p)**
> 注册并完成实名认证，**双方均可获得 ¥16 元代金券**！

---

## ✅ 核心优势

| 特性 | 说明 |
|------|------|
| **💰 超低价格** | Qwen 视觉模型仅需 ¥0.5/百万 tokens |
| **🆓 免费模型** | 多款模型完全免费使用 |
| **🚀 国内直连** | 无需科学上网，延迟极低 |
| **🎯 兼容 OpenAI** | 标准 API 格式，无缝对接 |

---

## 📝 注册步骤

### 1. 访问官网（使用邀请链接）

打开 [https://cloud.siliconflow.cn/i/OVI2n57p](https://cloud.siliconflow.cn/i/OVI2n57p)

> ⚠️ **重要**：使用邀请链接注册，双方都能获得 ¥16 代金券！

### 2. 注册并实名认证

- 手机号注册
- 完成实名认证（可获得代金券）

### 3. 获取 API Key

1. 登录后进入「账户设置」
2. 找到「API 密钥」
3. 点击「创建新密钥」
4. 复制生成的 API Key（格式：`sk-xxx...`）

### 4. 配置到项目

编辑 `docker-compose.yml` 文件：

```yaml
- API_KEY=sk-xxxxxxxx  # 粘贴你的 API Key
```

---

## 💰 费用估算

以本项目配置为例：

| 模型 | 用途 | 价格 | 估算消耗 |
|------|------|------|----------|
| Qwen/Qwen2.5-VL-32B-Instruct | 验证码识别 | ¥0.5/百万tokens | ~¥0.01/次任务 |
| Qwen/Qwen2.5-7B-Instruct | 文本任务 | 免费 | ¥0 |

**结论**：¥16 代金券可完成 **约 1500+ 次领取任务**

---

## 🎯 推荐模型

本项目已预配置以下模型：

### 验证码模型（视觉识别）
- **主模型**: `Qwen/Qwen2.5-VL-32B-Instruct` - 性价比最高
- **备用**: `Qwen/Qwen2.5-VL-72B-Instruct` - 更强大

### 主力模型（文本任务）
- **主模型**: `Qwen/Qwen2.5-7B-Instruct` - 免费
- **备用**: `Qwen/Qwen2.5-72B-Instruct`

---

## ❓ 常见问题

### Q: 可以使用其他 API 提供商吗？
A: 本项目针对 SiliconFlow 优化，如需使用其他提供商，需修改 API 地址和模型配置。

### Q: 免费额度够用吗？
A: 验证码模型收费极低（¥0.5/百万tokens），主力模型免费，日常使用成本几乎为零。

### Q: 如何查看余额？
A: 登录 SiliconFlow 控制台，可以查看余额和使用明细。

### Q: API Key 安全吗？
A: 请勿在公开场合泄露 API Key。本项目所有配置文件仅供本地使用。

---

## 🔗 相关链接

- [SiliconFlow 官网（邀请链接）](https://cloud.siliconflow.cn/i/OVI2n57p) - 注册双方可获 ¥16 代金券
- [项目公益站点](https://epic.910501.xyz/) - 免费体验
- [项目 README](../README.md)
