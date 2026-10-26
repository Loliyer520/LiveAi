# API 插件

给 bot 加能力的声明式插件：**不用写代码、不用改提示词、不用重启**。每个插件一个目录，
主AI 在聊天里用 `plugin_manage`（list / enable / disable / reload）管理，管理员改完文件后
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
- `list`：渲染数组——`from` 支持 `data.volumes[].chapters` 这种 `[]` 展平写法；`item` 是每条记录的模板
  （`{{字段}}` 先从记录取，取不到回退参数，再取不到渲染为空）；`max` 数字或 `{{参数}}`（默认 20，封顶 300），
  超出会附"共 N 条只列前 M 条"；`separator` 默认 `\n`
- `code_path` / `ok_value` / `msg_path`：HTTP 200 但业务失败的接口（如 `{"code":0,"error":"…"}`）——
  `code_path` 指向的字段不等于 `ok_value`（默认 0）时判定失败，错误文案取 `msg_path`（默认 `msg`）
- `save`：`{ "from": "data.content", "path": "novels/{{book_id}}.txt", "html_to_text": true, "send": "{{forward}}" }` —— 把响应里
  某个子树存进本会话工作区（对象存成 JSON，字符串原样；`html_to_text` 去 `<p>`/`<br>` 标签和实体），
  之后可用 file_read / file_search / send_file 处理。`send` 插值结果为真（true/1/yes/是）时，
  存完立刻把全文逐条原文发给用户（单次最多 150 行，超出提示用 send_file 续发）——**转发长原文
  （小说章节等）的正确姿势：插件工具参数里带个 forward 开关接 save.send，一次调用完成
  取内容→存 txt→直发，别让模型复述。** 发整文件也可以用内置 send_file 工具。
- 可选参数的 `default` 会被自动填上（如 `"count": {"type": "number", "default": 120}`）
- `format: "text"`：返回原始正文

## 护栏（框架强制，插件无法绕过）

- 所有请求走 SSRF 检查：内网/回环/链路本地地址拒绝（DNS 解析也算）。
  **例外**：清单里给工具写 `"allow_internal": true` 可访问本机/内网服务（如本机 7910 的
  番茄小说 API）——只能写在磁盘上的 plugin.json 里，模型无法伪造，相当于号主手动放行
- 每次调用占用当回合网络预算（每回合 8 次）
- 响应超 512KB 拒收；单插件加载失败只隔离它自己，`plugin_manage list` 里能看到原因
- 工具名与内置工具冲突时插件工具被跳过（内置优先）

现成示例：`weather/`（wttr.in 免 key 天气）、`novel/`（本机 7910 番茄小说：搜索/目录/正文，
演示 allow_internal、list 展平、code_path 业务错误、save 存盘 + html_to_text + send 一键转发）。
