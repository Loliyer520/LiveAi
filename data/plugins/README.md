# API 插件

给冰糖加能力的声明式插件：**不用写代码、不用改提示词、不用重启**。每个插件一个目录，
主AI 在聊天里用 `plugin_manage`（list / enable / disable / reload）管理，卡西改完文件后
让主AI 调一次 `plugin_manage action=reload` 即热加载。

## 目录结构

```
data/plugins/
├── <插件名>/
│   └── plugin.json     # 清单（见下）
├── secrets.json        # 可选，API key 仓库：{"KEY名": "值"}
└── _state.json         # 自动生成，enable/disable 状态
```

## plugin.json 格式

```json
{
  "name": "插件名（小写标识符）",
  "description": "一句话说明",
  "enabled": true,
  "tools": [
    {
      "name": "tool_name",
      "description": "给模型看的说明（什么时候用、参数怎么填）",
      "master_only": false,
      "parameters": { "type": "object", "properties": { "city": {"type": "string"} }, "required": ["city"] },
      "request": {
        "method": "GET",
        "url": "https://api.example.com/weather?city={{city}}&key={{secrets.WEATHER_KEY}}",
        "headers": { "x-token": "{{secrets.WEATHER_KEY}}" },
        "body": "{\"q\": \"{{city}}\"}"
      },
      "response": {
        "format": "json",
        "extract": "data.list[0]",
        "template": "{{city}}：{{data.temp}}℃"
      }
    }
  ]
}
```

## 插值占位符

- `{{参数名}}` — 工具参数；URL 里自动 URL-encode，headers/body 里原样替换
- `{{secrets.KEY}}` — 取 secrets.json 里的值；**key 不会暴露给模型**，响应里若出现密钥会自动打码成 `***`
- `{{scope.type}}` / `{{scope.id}}` — 当前会话类型/ID

## 响应处理（response 段，可省）

- `format: "json"`（默认）：`extract` 用点路径取子树（`a.b[0].c` 写法），省略则返回整个 JSON（截断 4000 字符）
- `template`：优先于 `extract`，`{{点路径}}` 从响应 JSON 取值，取不到时回退到参数
- `format: "text"`：返回原始正文

## 护栏（框架强制，插件无法绕过）

- 所有请求走 SSRF 检查：内网/回环/链路本地地址拒绝（DNS 解析也算）
- 每次调用占用当回合网络预算（每回合 8 次）
- 响应超 512KB 拒收；单插件加载失败只隔离它自己，`plugin_manage list` 里能看到原因
- 工具名与内置工具冲突时插件工具被跳过（内置优先）

现成示例：`weather/`（wttr.in 免 key 天气）。
