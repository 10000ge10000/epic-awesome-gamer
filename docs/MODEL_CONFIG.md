# AI 模型配置

Epic Kiosk 使用 OpenAI-compatible API 处理文本判断和 hCaptcha 视觉识别。项目内部会把自动化流程中的模型调用转换为 `/v1/chat/completions` 请求，因此 Provider 只要兼容该接口即可接入。

## 推荐配置

### NVIDIA（当前生产推荐）

```env
API_PROVIDER=nvidia
API_BASE_URL=https://integrate.api.nvidia.com/v1
API_KEY=<your-nvidia-api-key>
CAPTCHA_MODEL=meta/llama-4-maverick-17b-128e-instruct
CAPTCHA_MODEL_FALLBACK=meta/llama-4-maverick-17b-128e-instruct
CAPTCHA_API_TIMEOUT=60
HCAPTCHA_EXECUTION_TIMEOUT=240
HCAPTCHA_RESPONSE_TIMEOUT=120
HCAPTCHA_PAYLOAD_TIMEOUT=90
```

适用场景：验证码视觉识别稳定性优先，生产部署优先使用。

### SiliconFlow（兼容部署）

```env
API_PROVIDER=siliconflow
API_BASE_URL=https://api.siliconflow.cn/v1
API_KEY=<your-siliconflow-api-key>
CAPTCHA_MODEL=Qwen/Qwen3-VL-32B-Instruct
CAPTCHA_MODEL_FALLBACK=Qwen/Qwen3-VL-30B-A3B-Instruct
CAPTCHA_API_TIMEOUT=60
```

适用场景：使用一键脚本或已有 SiliconFlow 账号。具体模型可用性和价格以 Provider 控制台为准。

### 自定义 OpenAI-compatible 网关

```env
API_PROVIDER=custom
API_BASE_URL=https://your-gateway.example.com/v1
API_KEY=<your-provider-api-key>
CAPTCHA_MODEL=<vision-capable-model>
CAPTCHA_MODEL_FALLBACK=<vision-capable-fallback-model>
```

自定义网关必须支持图片输入，并返回兼容 OpenAI Chat Completions 的响应结构。

## 关键环境变量

| 变量 | 作用 |
| --- | --- |
| `API_PROVIDER` | 日志和部署标识，例如 `nvidia`、`siliconflow`、`custom`。 |
| `API_BASE_URL` | OpenAI-compatible API 根地址，通常以 `/v1` 结尾。 |
| `API_KEY` | Provider API Key，只能写入 `.env`。 |
| `CAPTCHA_MODEL` | 验证码视觉识别主模型。 |
| `CAPTCHA_MODEL_FALLBACK` | 验证码视觉识别备用模型。 |
| `CAPTCHA_API_TIMEOUT` | 单次验证码模型请求超时时间。 |
| `CAPTCHA_PROVIDER` | 外部验证码服务商兜底，默认 `none`。 |
| `INTERNAL_API_TOKEN` | Web 与 Worker 内部接口共享密钥，必须独立随机生成。 |

## 模型选择逻辑

- 包含图片的验证码任务使用 `CAPTCHA_MODEL`。
- 验证码模型调用失败时，会尝试 `CAPTCHA_MODEL_FALLBACK`。
- 非验证码流程判断使用 Compose 中配置的主力文本模型。

## 安全要求

- 不要把 `API_KEY`、`CAPTCHA_PROVIDER_API_KEY` 或 `INTERNAL_API_TOKEN` 写入 Git 跟踪文件。
- 不要在日志、截图、Issue 或 PR 中公开完整 `.env`。
- 切换 Provider 后需要重建并重启 Worker：

```bash
docker compose build worker && docker compose up -d worker
```

## 排查建议

- `401` / `403`：Key 无效、权限不足、账号额度不可用。
- `404`：模型 ID 不存在，或当前账号没有调用权限。
- 请求超时：提高 `CAPTCHA_API_TIMEOUT`，同时检查 WARP 出口和 Provider 网络质量。
- 识别失败：优先更换视觉模型，再考虑启用外部验证码服务商兜底。
