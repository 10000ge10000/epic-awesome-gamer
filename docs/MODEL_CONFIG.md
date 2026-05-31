# 🤖 AI 模型配置说明

## 📋 双模型架构

本项目采用**智能双模型**策略，针对不同任务场景使用不同的 AI 模型，并具备自动故障切换能力。

---

## 🎯 主力模型（Primary Model）

### 用途
处理除验证码外的所有任务，包括：
- 页面元素识别
- 按钮定位
- 流程判断
- 文本理解

### 配置
```python
主模型: Qwen/Qwen2.5-7B-Instruct（免费）
备用模型: Qwen/Qwen2.5-72B-Instruct
```

### 切换逻辑
```
Qwen2.5-7B-Instruct 调用失败
    ↓
自动切换到 Qwen2.5-72B-Instruct
    ↓
记录日志并继续执行
```

---

## 🔐 验证码模型（Captcha Model）

### 用途
专门处理 hCaptcha 验证码识别任务：
- 图像分类
- 物体识别
- 验证码破解

### 配置
```python
主模型: Qwen/Qwen2.5-VL-32B-Instruct
备用模型: Qwen/Qwen2.5-VL-72B-Instruct
```

### 切换逻辑
```
Qwen2.5-VL-32B-Instruct 调用失败
    ↓
自动切换到 Qwen2.5-VL-72B-Instruct
    ↓
记录日志并继续执行
```

---

## ⚙️ 技术实现

### 模型自动识别任务类型
系统会根据 API 调用的 `contents` 参数自动判断：
- **包含图片数据** → 使用验证码模型
- **纯文本请求** → 使用主力模型

---

## 📊 模型对比

| 模型名称 | 类型 | 价格 | 适用场景 |
|---------|------|------|---------|
| Qwen2.5-7B-Instruct | 文本 | **免费** | 文本任务 |
| Qwen2.5-72B-Instruct | 文本 | ¥2/百万tokens | 备用 |
| Qwen2.5-VL-32B-Instruct | 视觉 | ¥0.5/百万tokens | 验证码识别 |
| Qwen2.5-VL-72B-Instruct | 视觉 | ¥4/百万tokens | 备用 |

---

## 🔧 配置位置

### 模型配置（已写死）

所有模型配置硬编码在 `app/settings.py` 中：

```python
# 主力模型配置
PRIMARY_MODEL: str = Field(default="Qwen/Qwen2.5-7B-Instruct")
PRIMARY_MODEL_FALLBACK: str = Field(default="Qwen/Qwen2.5-72B-Instruct")

# 验证码模型配置
CAPTCHA_MODEL: str = Field(default="Qwen/Qwen2.5-VL-32B-Instruct")
CAPTCHA_MODEL_FALLBACK: str = Field(default="Qwen/Qwen2.5-VL-72B-Instruct")
```

**注意**: 这些配置已经过优化测试，建议不要修改。

### 🔑 API Key 配置（唯一需要修改）

在 `docker-compose.yml` 中修改：

```yaml
environment:
  - API_KEY=sk-xxx  # 修改为你的 SiliconFlow API Key
```

**重要说明**：
- ✅ 仅支持 [SiliconFlow](https://cloud.siliconflow.cn/i/OVI2n57p) 的 API Key
- 🆓 SiliconFlow 免费提供 Qwen2.5-7B-Instruct 模型
- 🎁 使用邀请链接注册可获 ¥16 代金券

---

## 💰 费用估算

| 项目 | 价格 | 说明 |
|------|------|------|
| 主力模型 | **免费** | Qwen2.5-7B-Instruct |
| 验证码模型 | ¥0.5/百万tokens | Qwen2.5-VL-32B-Instruct |
| ¥16 代金券 | ≈ **1500+ 次任务** | 每次任务约 ¥0.01 |

---

## 📝 日志示例

### 正常运行
```
🎯 API 提供商: siliconflow
🔐 验证码模型: Qwen/Qwen2.5-VL-32B-Instruct (备用: Qwen/Qwen2.5-VL-72B-Instruct)
🤖 主力模型: Qwen/Qwen2.5-7B-Instruct (备用: Qwen/Qwen2.5-72B-Instruct)
```

### 自动切换
```
❌ 主力模型 Qwen2.5-7B-Instruct 故障，自动切换到 Qwen2.5-72B-Instruct
⚠️ 主力模型已切换到备用
```

---

## ❓ 常见问题

### Q: 可以使用其他 API 提供商吗？
A: 本项目针对 SiliconFlow 优化，如需使用其他提供商，需修改 API 地址和模型配置。

### Q: 可以手动指定模型吗？
A: 不建议。当前配置已经过充分测试，能够应对各种场景。

### Q: 如果备用模型也失败了怎么办？
A: 系统会抛出异常并记录详细日志，建议检查 API Key 和网络连接。

### Q: 如何查看当前使用的模型？
A: 查看容器日志：
```bash
docker logs epic-worker -f
```

---

## 🔗 相关文档

- [SiliconFlow 官网（邀请链接）](https://cloud.siliconflow.cn/i/OVI2n57p) - 注册双方可获 ¥16 代金券
- [项目主 README](../README.md)
