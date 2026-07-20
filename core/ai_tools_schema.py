import copy

# 循环型工具：执行后把 tool_result 回填给模型继续本回合
LOOP_TOOL_NAMES = {'memory_list', 'memory_get', 'memory_add', 'memory_update', 'web_search', 'list_tasks', 'get_task', 'download_file', 'check_github_version', 'execute_update', 'create_agent', 'send_to_agent', 'peek_agent', 'list_agents', 'destroy_agent', 'create_recurring_task', 'list_recurring_tasks', 'update_recurring_task', 'delete_recurring_task', 'view_image', 'list_stickers', 'annotate_sticker', 'send_sticker', 'send_local_image', 'send_file', 'manage_upstream', 'manage_channel', 'manage_role', 'query_logs', 'manage_mute'}

# 指令型工具：终结本回合，由运行时按结构化入参执行
DIRECTIVE_TOOL_NAMES = {'send_message', 'remember', 'notify_master', 'create_task', 'recall_message', 'stay_silent'}

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
    'view_image': {
        'name': 'view_image',
        'description': (
            '查看/解析本次触发消息里的某张图片，返回该图片的文字描述。'
            '只有当你确实需要看懂图片内容才有意义时才调用（比如别人发图问你、图里有你需要理解的文字/梗/信息）；'
            '纯表情、刷屏、跟你无关的图不用看。'
            'index 从 1 开始，对应背景信息里"本次消息包含的图片"列出的序号；'
            '不传 index 时默认解析第 1 张。'
            '可选 question 用来指定你想重点看什么（比如"图里的文字是什么""这个人在做什么"），不传则做通用描述。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'index': {'type': 'integer', 'description': '要查看的图片序号，从 1 开始，默认 1'},
                'question': {'type': 'string', 'description': '可选，你想重点了解图片的什么内容'},
            },
            'required': [],
        },
    },
    'list_stickers': {
        'name': 'list_stickers',
        'description': (
            '列出你自己账号收藏的表情包（QQ 收藏表情），返回带序号的列表，每条包含你之前给它打的备注（如果有）。'
            '想发表情包前先用它看看有哪些、序号是多少。'
            '如果某个表情还没备注，建议先用 annotate_sticker 给它打个备注（描述这个表情是什么、适合什么场景），方便以后凭记忆挑选。'
            '这份列表是账号级共享缓存（几分钟内有效），不同会话看到的序号一致，一般不需要强制刷新；'
            '只有怀疑收藏内容变了（比如刚在别处收藏/删除了表情）才传 refresh=true 强制重新拉取。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'refresh': {'type': 'boolean', 'description': '是否强制重新拉取，忽略缓存，默认 false'},
            },
            'required': [],
        },
    },
    'annotate_sticker': {
        'name': 'annotate_sticker',
        'description': (
            '给你收藏的某个表情包打备注或改备注，方便以后按备注挑选要发哪个。'
            'index 从 1 开始，对应 list_stickers 列出的序号。'
            'note 写清楚这个表情是什么样子、表达什么情绪、适合什么场景。'
            '调用前请先用 list_stickers 确认序号。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'index': {'type': 'integer', 'description': '要打备注的表情序号，从 1 开始（来自 list_stickers）'},
                'note': {'type': 'string', 'description': '备注内容，描述这个表情的样子/情绪/适用场景'},
            },
            'required': ['index', 'note'],
        },
    },
    'send_sticker': {
        'name': 'send_sticker',
        'description': (
            '把你收藏的某个表情包发到当前会话。index 从 1 开始，对应 list_stickers 列出的序号。'
            '在合适的聊天氛围里用表情包活跃气氛或表达情绪，但别刷屏。'
            '调用前请先用 list_stickers 确认序号（尤其是你还没查过收藏列表时）。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'index': {'type': 'integer', 'description': '要发送的表情序号，从 1 开始（来自 list_stickers）'},
            },
            'required': ['index'],
        },
    },
    'send_local_image': {
        'name': 'send_local_image',
        'description': (
            '把一张本地图片文件发送到当前会话（例如把代码渲染成的图片发出去）。'
            '出于安全限制，只能发送项目 data/images/ 目录下的图片文件，'
            'path 传相对该目录的文件名（如 "code_abc.png"）或该目录下的绝对路径；'
            '目录以外的路径会被拒绝。支持 .png/.jpg/.jpeg/.gif/.webp，单张不超过 10MB。'
            '可选 caption 会作为图片附带的文字一起发送。'
            '发送成功后会返回 message_id。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': '要发送的本地图片路径，限定在项目 data/images/ 目录内'},
                'caption': {'type': 'string', 'description': '可选，随图片一起发送的文字说明'},
            },
            'required': ['path'],
        },
    },
    'send_file': {
        'name': 'send_file',
        'description': (
            '把服务器本地的一个文件通过 QQ 发送给当前会话（私聊或群聊）。'
            'path 必须是服务器上的绝对路径（如 /my/pro/bot/LiveAi/data/files/report.pdf）；'
            '可选 name 指定对方看到的显示文件名（含后缀），不传则取路径末尾的文件名。'
            '发送成功后返回确认信息。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': '服务器上文件的绝对路径'},
                'name': {'type': 'string', 'description': '可选，对方看到的显示文件名（含后缀）'},
            },
            'required': ['path'],
        },
    },
    'send_message': {
        'name': 'send_message',
        'description': (
            '发送消息给用户。这是唯一真正发送消息的方式——你输出的普通文字不会被发送。'
            '如果需要先思考，把思考写在 content 的 <thinking>...</thinking> 内；系统会自动过滤这部分，用户只看到标签外内容。'
            '收到用户请求时，优先快速回应确认，不要让用户等待。'
            '如果任务需要时间（如 dev_agent），先发消息告知用户"好的，马上帮你查/等我改一下"，'
            '然后任务会在后续轮次中处理（当用户继续说话或系统触发时）。'
            '发送成功后会返回 message_id，如果内容过时需要撤回可以调用 recall_message。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'content': {
                    'type': 'string',
                    'description': '要发送的消息内容；可以包含 <thinking>...</thinking> 思考区，发送前会自动移除；换行会被拆成多条独立消息分别发送，用换行分隔 1 到 3 条短句',
                },
            },
            'required': ['content'],
        },
    },
    'stay_silent': {
        'name': 'stay_silent',
        'description': (
            '本回合保持沉默、不发任何消息，直接结束这一轮。'
            '当你判断现在不该说话（没被点名、插不上话、没有明确要回应的内容、'
            '或说了反而尴尬）时调用它。'
            '注意：不要用它来“假装沉默却把想说的话写在别处”——真要说话就调用 send_message，'
            '真不想说才调用 stay_silent。二者只能选其一。'
        ),
        'input_schema': {'type': 'object', 'properties': {}, 'required': []},
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
            '或有需要跨会话协作的事项时调用。\n'
            'content 可以是一句自然语言（主AI会自行理解并协调），'
            '也可以是一段 JSON 字符串来精确表达意图，支持以下 request_type：\n'
            '1) 联系/转达他人：{"request_type":"coordinate_contact","target_scope_type":"private",'
            '"target_scope_id":"对方QQ号","content":"要转达的话","instruction":"如果合适，请主动联系并自然转达"}\n'
            '2) 设定某人的全局人物设定/关系（如“X是我女朋友”“对X语气好一点”）：'
            '{"request_type":"set_user_preference","target_query":"对方昵称或QQ",'
            '"preference_text":"要长期记住的设定，写清关系或对待方式"}\n'
            '3) 查询之前托付联系的进度（如“我让你发的消息对方回了吗”）：'
            '{"request_type":"query_contact_status"}（主AI会自动定位最近一次联系任务并回传进度）\n'
            '不确定用哪种时，直接用自然语言描述即可，主AI会判断。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'content': {'type': 'string', 'description': '要上报给主AI的内容，或上述 JSON 格式的协调请求'},
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
    'create_agent': {
        'name': 'create_agent',
        'description': (
            '创建一个常驻后台 agent 并立即让它开工，返回 agent_id。'
            '与一次性 dev_agent 任务不同，常驻 agent 会持续存在、可多轮双向沟通：'
            '它能读写本地项目代码、执行 shell、只读查阅或（token 权限允许时）改动 GitHub 仓库；'
            '干完一段会挂起待命，可以随时用 send_to_agent 追加指令、用 peek_agent 查进度、'
            '用 destroy_agent 结束它。适合需要长期跟进、分阶段推进或反复交互的后台工作。'
            'instruction 用自然语言写清楚要它做什么、期望结果，涉及仓库时带上 owner/repo。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'instruction': {'type': 'string', 'description': '交给该 agent 的任务描述，用自然语言写清楚要做什么、期望结果'},
            },
            'required': ['instruction'],
        },
    },
    'send_to_agent': {
        'name': 'send_to_agent',
        'description': (
            '给指定常驻 agent 发送一条消息/追加指令，唤醒它继续工作。'
            '用于在 agent 挂起待命或运行中补充要求、回答它的提问、调整方向。'
            'agent_id 来自 create_agent 的返回或 list_agents。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'agent_id': {'type': 'string', 'description': '目标 agent 的 ID'},
                'message': {'type': 'string', 'description': '要发给该 agent 的消息或指令内容'},
            },
            'required': ['agent_id', 'message'],
        },
    },
    'peek_agent': {
        'name': 'peek_agent',
        'description': (
            '获取指定常驻 agent 当前的进度总结（由一个只读、无工具权限的总结 AI 生成），'
            '不会打断它正在进行的工作。用于了解 agent 干到哪一步、有没有卡住或风险。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'agent_id': {'type': 'string', 'description': '要查看进度的 agent 的 ID'},
            },
            'required': ['agent_id'],
        },
    },
    'list_agents': {
        'name': 'list_agents',
        'description': (
            '列出当前所有常驻 agent 及其状态（running/waiting/idle）、任务摘要、消息数与时间。'
            '用于在创建/查看/销毁 agent 前先掌握全局情况。'
        ),
        'input_schema': {'type': 'object', 'properties': {}, 'required': []},
    },
    'destroy_agent': {
        'name': 'destroy_agent',
        'description': (
            '销毁指定常驻 agent：强制中断它的常驻循环并移除记录（会自动清理它的后台 shell 任务）。'
            'summarize=true 时会在销毁前先做一份总结（已完成的操作、可能遗留的隐患），随结果返回。'
            '确认某个 agent 不再需要时使用。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'agent_id': {'type': 'string', 'description': '要销毁的 agent 的 ID'},
                'summarize': {'type': 'boolean', 'description': '是否在销毁前先生成一份总结，默认 false'},
            },
            'required': ['agent_id', 'summarize'],
        },
    },
    'manage_upstream': {
        'name': 'manage_upstream',
        'description': '管理 API 上游配置（三级架构第一级）。支持查看/新增/修改/删除上游，以及查询上游余额。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'description': 'list | add | update | remove | balance', 'enum': ['list', 'add', 'update', 'remove', 'balance']},
                'name': {'type': 'string', 'description': '上游名称（add/update/remove/balance 时使用）'},
                'base_url': {'type': 'string', 'description': 'API 地址，如 https://api.example.com'},
                'api_key': {'type': 'string', 'description': 'API 密钥'},
                'messages_path': {'type': 'string', 'description': 'Anthropic 接口填 /v1/messages，OpenAI 留空'},
            },
            'required': ['action'],
        },
    },
    'manage_channel': {
        'name': 'manage_channel',
        'description': '管理渠道配置（三级架构第二级）。渠道是模型池，包含多个上游+模型的组合及轮询策略。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'description': 'list | add | update | remove | addmodel | removemodel', 'enum': ['list', 'add', 'update', 'remove', 'addmodel', 'removemodel']},
                'name': {'type': 'string', 'description': '渠道名称'},
                'strategy': {'type': 'string', 'description': '轮询策略: fallback（失败切换）| random（每次随机）| roundrobin（每次轮询）', 'enum': ['fallback', 'random', 'roundrobin']},
                'upstream': {'type': 'string', 'description': 'addmodel 时：上游名称'},
                'model_id': {'type': 'string', 'description': 'addmodel 时：模型 ID'},
                'model_index': {'type': 'integer', 'description': 'removemodel 时：要删除的模型序号（从0开始）'},
            },
            'required': ['action'],
        },
    },
    'manage_role': {
        'name': 'manage_role',
        'description': '管理角色-渠道绑定（三级架构第三级）。为 main/tiered/agent/dev_agent/vision 各角色指定使用哪个渠道。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'description': 'list | set', 'enum': ['list', 'set']},
                'role': {'type': 'string', 'description': 'set 时：角色名，可选 main / tiered / agent / dev_agent / vision'},
                'channel': {'type': 'string', 'description': 'set 时：要绑定的渠道名称'},
            },
            'required': ['action'],
        },
    },
    'query_logs': {
        'name': 'query_logs',
        'description': (
            '查询系统运行时日志。用于排查问题、了解后台 agent/task 状态、检查 API 调用情况、'
            '确认聊天 AI 触发原因等。\n'
            '参数：count（返回条数，1-200，默认20）、priority（过滤级别，0-5）、'
            'scope_key（会话标识，格式 "group:群号" 或 "private:QQ号"，默认当前会话）。\n'
            '优先级说明：0=全部 / 1=忽略API的info / 2=只看异常 / 3=agent日志 / 4/5=聊天AI日志。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'count': {'type': 'integer', 'description': '返回日志条数，1-200，默认20'},
                'priority': {'type': 'integer', 'description': '日志过滤级别 0-5，默认0（全部）'},
                'scope_key': {'type': 'string', 'description': '会话标识 "group:群号" 或 "private:QQ号"，默认当前会话'},
            },
            'required': [],
        },
    },
    'manage_mute': {
        'name': 'manage_mute',
        'description': (
            '群聊禁言管理工具。'
            '支持禁言（ban）、解除禁言（unban）、查看禁言状态（status）三种操作。'
            '仅在当前会话是群聊时可用，需要 bot 自身是该群的管理员或群主才能执行 ban/unban。'
            '\n'
            '参数说明：\n'
            '- action: ban（禁言）/ unban（解除禁言）/ status（查询成员角色和 bot 自身权限）\n'
            '- target_user_id: 要操作的群成员 QQ 号，ban/unban 时必填\n'
            '- duration: 禁言时长（秒），仅在 ban 时有效；默认 60 秒，最大 30 天（2592000 秒）\n'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'action': {
                    'type': 'string',
                    'description': '操作类型：ban=禁言, unban=解除禁言, status=查看状态',
                    'enum': ['ban', 'unban', 'status'],
                },
                'target_user_id': {
                    'type': 'integer',
                    'description': '要操作的群成员 QQ 号，ban/unban 时必填',
                },
                'duration': {
                    'type': 'integer',
                    'description': '禁言时长（秒），ban 时使用，默认 60，最大 2592000（30天）',
                },
            },
            'required': ['action'],
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
    allow_config_tools: bool = False,
    include_group_management: bool = False,
    cache_last: bool = True,
    immediate_mode: bool = False,
) -> list[dict]:
    names: list[str] = []
    if include_memory:
        names.extend(['memory_list', 'memory_get', 'memory_add', 'memory_update'])
    if allow_search:
        names.append('web_search')
    names.append('query_logs')
    if include_download_file:
        names.append('download_file')
    if include_remember:
        names.append('remember')
    if allow_notify_master:
        names.append('notify_master')
    if allow_tasks:
        names.append('create_task')
        names.extend(['create_agent', 'send_to_agent', 'peek_agent', 'list_agents', 'destroy_agent'])
    if allow_recurring_tasks:
        names.extend(['create_recurring_task', 'list_recurring_tasks', 'update_recurring_task', 'delete_recurring_task'])
    if allow_update_tools:
        names.extend(['check_github_version', 'execute_update'])
    if allow_config_tools:
        names.extend(['manage_upstream', 'manage_channel', 'manage_role'])
    if include_group_management:
        names.append('manage_mute')
    if include_message:
        names.append('send_message')
    if include_message and immediate_mode:
        names.append('recall_message')
        names.append('stay_silent')
        names.extend(['view_image', 'list_stickers', 'annotate_sticker', 'send_sticker', 'send_local_image', 'send_file'])
    tools = [copy.deepcopy(_TOOL_DEFINITIONS[name]) for name in names]
    if tools and cache_last:
        tools[-1]['cache_control'] = {'type': 'ephemeral'}
    return tools
