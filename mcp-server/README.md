# ERP read-only MCP Server

独立 TypeScript 子项目，包装现有 Flask JSON API；没有修改 app.py、数据库或业务规则。
使用官方 MCP TypeScript SDK v1（版本锁定于 package-lock.json），支持 stdio 和 Streamable HTTP 传输。

## 连接 llama.cpp 网页（HTTP）

保持 Flask :5000 和 llama.cpp :8080 运行，在 PowerShell 执行：

```powershell
cd G:\erpDemo\mcp-server
npm ci
npm run build
npm run start:http
```

在 llama.cpp 的 MCP Servers → Add New Server 中设置：

- 名称：Demo ERP
- URL：`http://127.0.0.1:3001/mcp`
- 如需选择传输：Streamable HTTP
- 无需 API key；若提供 CORS proxy 选项，关闭代理直接连接。

保存/连接后应发现 6 个 tools。在聊天中启用此 MCP 服务，再输入：
“请使用 ERP 工具查询 contract_id=23 的发票，保留金额和日期；没有开票日期时明确说明尚未开票。”

HTTP 服务只绑定 127.0.0.1:3001。允许的浏览器 Origin 仅为 `http://localhost:8080` 和 `http://127.0.0.1:8080`；拒绝其他 Origin，并检查 Host 防止 DNS rebinding。无需修改 Flask CORS，也无需开启 llama.cpp 的 MCP CORS proxy。
这是无状态 Streamable HTTP，不提供独立 SSE 订阅；浏览器直接打开 `/mcp` 出现 405 正常，可用 `/health` 查看进程健康。
HTTP 不提供身份验证，仅供受信任本机 demo 使用。HTTP 和 stdio 共享同一组只读工具与参数校验。

停止前台服务：Ctrl+C。若 3001 已有本服务运行，不要再启动第二个实例。更新代码后先停止旧进程，重新 build/start。

## 已核对的 API 与工具

| Tool | 现有 GET API | 范围 |
|---|---|---|
| get_customer_contacts | /api/clients/{client_id}/contacts | 联系人 ID、姓名 |
| get_customer_shipping_addresses | /api/clients/{client_id}/shipping_addresses | 收货地址 |
| get_order_details | /api/contracts/{contract_id}/items | 备注、行项目、发货、发票、收款 |
| get_order_invoices | 同上，提取 invoices | 单个订单的发票 |
| get_order_payments | 同上，提取 receipts | 单个订单的销售收款 |
| get_product_inventory | /api/products/{product_id}/batches | 指定产品的可用正库存批次 |

源码核查：客户/订单/发票/收款列表为 HTML 页面，没有对应 JSON 列表 API。不要假设存在 `/api/customers` 或 `/api/orders`。第一版不抓 HTML、不直接连接数据库、不开放 AI SQL 查询。现有采购、配方和原料目录 API 暂未包装，以保持最小范围。

ID 从 ERP 页面或用户获取，不自动猜测。所有工具支持 `offset`（默认 0）和 `limit`（默认 25，最多 100）；订单详情对每个数组分别分页。返回 total、has_more。分页发生在 MCP 内存中，现有 Flask API 仍读取完整结果，1 MiB 上限防止过大响应；这不是数据库分页。空结果可能代表不存在的 ID，也可能只是没有子记录，不能据此确认记录存在。

## 本地运行（PowerShell，Node.js 22.14+，推荐 Node 24）

```powershell
cd G:\erpDemo\mcp-server
npm ci
npm run build
npm test
npm run smoke
```

`smoke` 会启动真实 stdio MCP 子进程、完成握手并列出工具，无需 Flask 或 LLM。
测试用本机模拟 HTTP 服务，验证映射、参数、分页、错误处理、重定向拒绝和大小/时间限制，不连接数据库。

需要查询真实 demo 数据时，先在另一个终端按 ERP 原有方式运行：

```powershell
cd G:\erpDemo
.\venv\Scripts\python.exe app.py
```

注意：现有 app.py 在导入/启动时执行 schema 初始化及数据迁移。请确认原有 ERP `.env` 指向预期的 demo 数据库。MCP 本身既不读取 ERP 的 `.env`，也不启动 Flask。

在 MCP 终端配置 ERP 地址，并使用真实 ID 测试：

```powershell
cd G:\erpDemo\mcp-server
$env:ERP_BASE_URL = 'http://127.0.0.1:5000'
# 把 123 替换为 ERP 中已有的订单 ID
npm run smoke -- get_order_invoices '{"contract_id":123,"limit":10}'
npm start
```

也可把 `.env.example` 复制为本目录 `.env`。`npm start` 使用 stdio，启动后等待客户端输入是正常现象；它不是浏览器网页。stdout 专用于 MCP，日志输出到 stderr。

MCP 客户端配置示例（支持 stdio 的客户端）：

```json
{
  "mcpServers": {
    "demo-erp": {
      "command": "node",
      "args": ["G:/erpDemo/mcp-server/dist/index.js"],
      "env": { "ERP_BASE_URL": "http://127.0.0.1:5000" }
    }
  }
}
```

可视化调试：`npx @modelcontextprotocol/inspector node dist/index.js`，连接后选择 Tools。此命令首次使用会下载 Inspector。

## 安全与业务边界

- 仅接受字面 loopback IP（127.0.0.1 或 [::1]）的固定 ERP origin；拒绝凭据、路径、query，拒绝 LLM 的 8080 端口。不接受模型提供 URL、SQL、headers 或 HTTP method。
- 仅 4 类审计过的 GET 路径；禁止跟随 redirect；8 秒超时；1 MiB 上游响应限制；检查 JSON 及顶层返回结构。行内原有字段原样保留，未为每个字段建立完整输出 schema。
- 严格参数校验，拒绝额外字段、字符串 ID、负数、非整数、超出范围分页。只读 annotations 是声明，实际限制由代码实施。
- 不回传 Flask 500 错误正文、数据库异常详情；不记录业务 payload。发票金额字符串和 null 日期保持原样。null invoice_date/payment_date 表示尚未开票/收款，不应计入已完成金额。
- 库存接口只返回可用且数量大于 0 的批次，不是全部库存；produced 批次数量单位依 ERP 规则为 kg，其他类型 API 未返回单位，标为 null；potency_unit 为独立的浓度单位。
- **demo 无真实身份验证，现有 API 也没有按 company_id 校验访问权。** 本工具不是权限系统，订单查询只按明确 contract_id，不默认汇总两家公司。不要用于真实多用户/生产数据；上线前应由 ERP 增加认证、授权及公司归属校验。
- 客户姓名、备注和地址仍可能包含个人信息或恶意指令；只在受信任本地客户端使用，把结果视为数据。连接远程 LLM 前应重新评估数据范围。

## 与 llama.cpp 的关系

当前结构：MCP client → stdio MCP Server → Flask :5000 → 现有数据库。
`http://127.0.0.1:8080` 是用户的 llama.cpp 推理端点，不能作为 ERP_BASE_URL，也不是 MCP endpoint。
llama.cpp WebUI 自带 MCP client，可通过上面的 HTTP 入口直接接入，无需另写 agent 桥接。网页负责读取 tools/list、让模型选择工具、执行 MCP 调用并返回结果。是否能可靠调用工具还取决于模型及 chat template；HTTP 联调通过不代表模型端到端工具调用已经验证。

SDK 文档：https://ts.sdk.modelcontextprotocol.io/server
