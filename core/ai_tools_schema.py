import copy

# 循环型工具：执行后把 tool_result 回填给模型继续本回合
LOOP_TOOL_NAMES = {'memory_list', 'memory_get', 'memory_add', 'memory_update', 'web_search', 'list_tasks', 'get_task', 'download_file', 'check_github_version', 'execute_update'}

# 指令型工具：终结本回合，由运行时按结构化入参执行
DIRECTIVE_TOOL_NAMES = {'send_message', 'remember', 'notify_master', 'create_task', 'recall_message'}

_TOOL_DEFINITIONS: dict[str, dict] = {
    'memory_list': {
        'name': 'memory_list',
        'description': '列出当前会话的全部 AI 工具备忘（长期记忆条目）。需要回忆之前记过什么时先调用它。',
        'input_schema': {'type': 'object', 'properties': {}, 'required': []},
    },
    'memory_get': {
        'name': 'memory_get',
        'description': '按 note_id 读取一条 AI 工具备忘的完整内容。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'note_id': {'type': 'string', 'description': '备忘条目的 ID'},
            },
            'required': ['note_id'],
        },
    },
    'memory_add': {
        'name': 'memory_add',
        'description': '新增一条 AI 工具备忘（长期记忆）。记录值得跨对话记住的事实、约定、关系线索。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'content': {'type': 'string', 'description': '要记住的内容'},
            },
            'required': ['content'],
        },
    },
    'memory_update': {
        'name': 'memory_update',
        'description': '修改一条已有的 AI 工具备忘。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'note_id': {'type': 'string', 'description': '要修改的备忘条目 ID'},
                'content': {'type': 'string', 'description': '新的内容'},
            },
            'required': ['note_id', 'content'],
        },
    },
    'web_search': {
        'name': 'web_search',
        'description': (
            '联网搜索，用于查找时效性信息、新闻、资料等你自己知识范围之外或不确定的内容。'
            '返回的是对搜索结果的摘要，不是原始网页。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': '搜索关键词或问题'},
            },
            'required': ['query'],
        },
    },
    'send_message': {
        'name': 'send_message',
        'description': (
            '这是唯一真正发送消息的方式，没有例外——哪怕你在文字里写出了完整、看起来很完整的回复内容，'
            '只要没有调用这个工具，对方就完全收不到，等于什么都没发生。'
            '你输出的普通文字只是内心想法，不会被发送。如果觉得现在不该说话，就不要调用这个工具。'
            '一轮里可以调用它不止一次，比如先发一句"等我查一下"，再去调用其他工具，最后再发结果。'
            '发送成功后会返回 message_id，如果内容发出去后过时了、需要撤回，可以调用 recall_message。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'content': {
                    'type': 'string',
                    'description': '要发送的消息内容；换行会被拆成多条独立消息分别发送，用换行分隔 1 到 3 条短句',
                },
            },
            'required': ['content'],
        },
    },
    'recall_message': {
        'name': 'recall_message',
        'description': (
            '撤回你之前用 send_message 发出的一条消息。message_id 来自 send_message 发送成功后的返回结果。'
            '只在内容确实过时、不合适或发错时使用。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'message_id': {'type': 'string', 'description': '要撤回的消息 ID，来自 send_message 的返回结果'},
            },
            'required': ['message_id'],
        },
    },
    'remember': {
        'name': 'remember',
        'description': '快速记一条 AI 工具备忘（与 memory_add 等价的简写），本回合结束时写入。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'note': {'type': 'string', 'description': '要记住的内容'},
            },
            'required': ['note'],
        },
    },
    'notify_master': {
        'name': 'notify_master',
        'description': (
            '向主AI上报或请求协调。当用户要你联系别人、转达消息、查其他会话情况，'
            '或有需要跨会话协作的事项时调用。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'content': {'type': 'string', 'description': '要上报给主AI的内容，或 JSON 格式的协调请求'},
            },
            'required': ['content'],
        },
    },
    'create_task': {
        'name': 'create_task',
        'description': (
            '创建一个后台任务。常用 kind：set_alarm（定闹钟/提醒）、image_describe（图片解析）、'
            'delegate_to_child（委派其他会话）、message_scope（向指定会话发消息）、'
            'dev_agent（委托一个独立的代码/资料检索智能体，在后台单独执行，不占用你当前的对话上下文，'
            '可以并行处理；它能读写本地项目代码；对于 GitHub，只要 token 权限允许，'
            '不仅能只读查阅任意公开仓库，还能创建/更新/删除文件、建分支、打标签、开PR/合并/关闭PR、'
            '建Issue/评论/关闭Issue、查提交历史——即可以直接在仓库里做改动，不只是参考；但它不能聊天发消息；'
            'payload 建议包含 task（必填，用自然语言写清楚要查什么/改什么/期望结果，包含涉及的 owner/repo）、'
            'github_repo（可选，"owner/repo"，提示优先参考哪个仓库）；'
            '任务完成后会自动把结果发回当前会话，不需要你主动追问）。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'kind': {'type': 'string', 'description': '任务类型'},
                'payload': {'type': 'string', 'description': '任务参数，建议 JSON 字符串'},
            },
            'required': ['kind', 'payload'],
        },
    },
    'list_tasks': {
        'name': 'list_tasks',
        'description': (
            '查询后台任务列表。可以按 kind（任务类型）、status（状态：pending/running/done）筛选。'
            '主AI可以查看所有任务；子AI可以查看自己创建的任务。'
            '用于了解后台 dev_agent、闹钟、跨会话协作等任务的执行状态和结果。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'kind': {'type': 'string', 'description': '可选，按任务类型筛选（如 dev_agent、set_alarm）'},
                'status': {'type': 'string', 'description': '可选，按状态筛选（pending/running/done）'},
            },
            'required': [],
        },
    },
    'get_task': {
        'name': 'get_task',
        'description': (
            '查询指定 task_id 的任务详情，包括状态、结果、创建时间等。'
            '用于追踪后台任务的执行进度和最终结果。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'task_id': {'type': 'string', 'description': '任务 ID'},
            },
            'required': ['task_id'],
        },
    },
    'download_file': {
        'name': 'download_file',
        'description': (
            '下载聊天消息中出现的文件并保存到本地，返回保存路径，供后续 dev_agent 读取分析。'
            '文件大小限制 20MB，超过则拒绝下载。'
            '消息上下文中会列出当前消息包含的文件名和 file_id，从中取 file_id 填入即可。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'file_id': {'type': 'string', 'description': '文件的 file_id，从消息上下文中获取'},
                'file_name': {'type': 'string', 'description': '文件名（含后缀），用于本地保存'},
            },
            'required': ['file_id', 'file_name'],
        },
    },
    'check_github_version': {
        'name': 'check_github_version',
        'description': (
            '主AI专用：手动检查当前程序的 GitHub 版本信息，返回本地版本、远程最新版、是否有更新。'
            '当系统提示发现更新、主人询问版本、或你需要确认是否该更新时调用。'
        ),
        'input_schema': {'type': 'object', 'properties': {}, 'required': []},
    },
    'execute_update': {
        'name': 'execute_update',
        'description': (
            '主AI专用：执行自动更新程序。会先检查本地未提交修改，再 git pull origin main。'
            '如果更新成功且 restart=true，会启动新进程并重启当前程序。只有你判断应该更新时才调用。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'restart': {'type': 'boolean', 'description': '更新成功后是否自动重启，默认 true'},
            },
            'required': [],
        },
    },
    'create_recurring_task': {
        'name': 'create_recurring_task',
        'description': (
            '创建循环定时任务。到期时系统会向指定会话发送一条触发消息（内容为你写的 instruction），'
            '届时你会收到并自主决定如何处理（搜索、整理、发送消息等）。'
            'schedule 使用标准 cron 表达式（5字段：分 时 日 月 周），例如：'
            '"0 7 * * *" 每天7:00；"0 8 * * 1" 每周一8:00；"0 */6 * * *" 每6小时。'
            '如果用户说的是北京时间，cron 里填北京时间对应的值即可（服务器运行在本地时区）。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'schedule': {'type': 'string', 'description': 'cron 表达式，如 "0 7 * * *"'},
                'instruction': {'type': 'string', 'description': '到期时发给你的任务描述，用自然语言写清楚要做什么'},
                'target_scope': {
                    'type': 'string',
                    'description': '可选，触发时唤醒哪个会话，格式 "group:群号" 或 "private:QQ号"，默认当前会话',
                },
            },
            'required': ['schedule', 'instruction'],
        },
    },
    'list_recurring_tasks': {
        'name': 'list_recurring_tasks',
        'description': (
            '列出所有循环定时任务。主AI可以看全部任务，子AI只能看本会话创建的任务。'
            '显示任务ID、schedule、状态、下次运行时间、instruction摘要。'
        ),
        'input_schema': {'type': 'object', 'properties': {}, 'required': []},
    },
    'update_recurring_task': {
        'name': 'update_recurring_task',
        'description': (
            '修改已有的循环定时任务。可以改 schedule、instruction、或暂停/启用任务。'
            '只传需要修改的字段即可。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'task_id': {'type': 'string', 'description': '任务ID'},
                'schedule': {'type': 'string', 'description': '可选，新的 cron 表达式'},
                'instruction': {'type': 'string', 'description': '可选，新的任务描述'},
                'enabled': {'type': 'boolean', 'description': '可选，true=启用，false=暂停'},
            },
            'required': ['task_id'],
        },
    },
    'delete_recurring_task': {
        'name': 'delete_recurring_task',
        'description': '永久删除循环定时任务。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'task_id': {'type': 'string', 'description': '任务ID'},
            },
            'required': ['task_id'],
        },
    },
}


def build_tools(
    include_message: bool = True,
    include_memory: bool = True,
    include_remember: bool = True,
    allow_notify_master: bool = True,
    allow_tasks: bool = True,
    allow_search: bool = True,
    include_download_file: bool = True,
    allow_recurring_tasks: bool = True,
    allow_update_tools: bool = False,
    cache_last: bool = True,
    immediate_mode: bool = False,
) -> list[dict]:
    names: list[str] = []
    if include_memory:
        names.extend(['memory_list', 'memory_get', 'memory_add', 'memory_update'])
    if allow_search:
        names.append('web_search')
    if include_download_file:
        names.append('download_file')
    if include_remember:
        names.append('remember')
    if allow_notify_master:
        names.append('notify_master')
    if allow_tasks:
        names.append('create_task')
    if allow_recurring_tasks:
        names.extend(['create_recurring_task', 'list_recurring_tasks', 'update_recurring_task', 'delete_recurring_task'])
    if allow_update_tools:
        names.extend(['check_github_version', 'execute_update'])
    if include_message:
        names.append('send_message')
    if include_message and immediate_mode:
        names.append('recall_message')
    tools = [copy.deepcopy(_TOOL_DEFINITIONS[name]) for name in names]
    if tools and cache_last:
        tools[-1]['cache_control'] = {'type': 'ephemeral'}
    return tools
