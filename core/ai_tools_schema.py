import copy

# 循环型工具：执行后把 tool_result 回填给模型继续本回合
LOOP_TOOL_NAMES = {'memory_list', 'memory_get', 'memory_add', 'memory_update', 'web_search', 'find_in_project', 'list_local_files', 'read_local_file', 'list_tasks', 'get_task', 'download_file', 'check_github_version', 'execute_update', 'create_agent', 'create_ssh_agent', 'list_ssh_profiles', 'manage_ssh_profile', 'validate_ssh_profile', 'send_to_agent', 'peek_agent', 'list_agents', 'destroy_agent', 'create_recurring_task', 'list_recurring_tasks', 'update_recurring_task', 'delete_recurring_task', 'view_image', 'list_stickers', 'annotate_sticker', 'send_sticker', 'view_sticker', 'send_local_image', 'send_voice', 'send_file', 'manage_upstream', 'manage_channel', 'manage_role', 'query_logs', 'manage_mute', 'qq_add_friend', 'qq_list_friend_requests', 'qq_approve_friend_request', 'qq_reject_friend_request', 'qq_join_group', 'qq_list_group_requests', 'qq_approve_group_request', 'qq_reject_group_request', 'qq_sync_contacts', 'validate_model_config', 'switch_agent_channel', 'relation_lookup', 'relation_list', 'relation_update_user', 'relation_add_fact', 'manage_knowledge_base', 'request_knowledge_base_update', 'set_thinking_level', 'set_session_mode', 'set_trigger_rate'}

# 指令型工具：终结本回合，由运行时按结构化入参执行
DIRECTIVE_TOOL_NAMES = {'send_message', 'remember', 'notify_master', 'create_task', 'create_tasker', 'recall_message', 'stay_silent'}

_TOOL_DEFINITIONS: dict[str, dict] = {
    'memory_list': {
        'name': 'memory_list',
        'description': '列出当前会话的全部 AI 工具备忘（长期记忆条目）。需要回忆之前记过什么、确认以前留过哪些约定/事实时先调用它，不要凭印象乱讲。',
        'input_schema': {'type': 'object', 'properties': {}, 'required': []},
    },
    'memory_get': {
        'name': 'memory_get',
        'description': '按 note_id 读取一条 AI 工具备忘的完整内容。适合在 memory_list 看见疑似相关条目后再展开确认细节。',
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
        'description': '新增一条 AI 工具备忘（长期记忆）。记录值得跨对话记住的事实、约定、关系线索；不要把短效情绪、当下实时状态当成长期记忆写进去。',
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
        'description': '修改一条已有的 AI 工具备忘。适合在旧记忆不完整、措辞不准、或需要把补充信息合并进去时使用。',
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
    'find_in_project': {
        'name': 'find_in_project',
        'description': (
            '只读搜索项目仓库。可按文件名通配或文件内容一次递归定位，返回项目相对路径和内容命中的行号。'
            '定位未知文件优先使用它，不要反复逐层读取目录。自动跳过依赖、构建产物和禁止访问目录。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'name_pattern': {'type': 'string', 'description': '可选，文件名通配模式；不含通配符时按不区分大小写的子串匹配'},
                'content_query': {'type': 'string', 'description': '可选，要在文本文件内容中搜索的文本或正则'},
                'is_regex': {'type': 'boolean', 'description': '是否把 content_query 当作正则表达式'},
                'subpath': {'type': 'string', 'description': '可选，限定搜索的项目相对子目录'},
                'max_results': {'type': 'integer', 'description': '可选，最多返回多少条结果，默认 40，上限 200'},
            },
            'required': [],
        },
    },
    'list_local_files': {
        'name': 'list_local_files',
        'description': '只读列出项目仓库指定目录下的文件和子目录。路径必须相对项目根目录；留空表示仓库根目录。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'subpath': {'type': 'string', 'description': '可选，项目相对目录；留空表示仓库根目录'},
            },
            'required': [],
        },
    },
    'read_local_file': {
        'name': 'read_local_file',
        'description': (
            '只读读取项目仓库中的 UTF-8 文本文件。路径必须相对项目根目录；禁止读取仓库外路径、敏感运行数据和二进制文件。'
            '文件超过 300000 字节时拒绝读取，请先搜索并缩小目标。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': '项目根目录下的相对文件路径'},
            },
            'required': ['path'],
        },
    },
    'set_thinking_level': {
        'name': 'set_thinking_level',
        'description': (
            '查看或设置当前会话的模型思考强度。'
            'level 可选值为 off、low、medium、high；不传 level 时只返回当前设置。'
            '平时建议保持 low；需要快速高频闲聊可切到 off，需要长推理/分析代码/写长文时再切到 medium 或 high。'
            '这是当前会话的运行时开关，只保存在内存里，重启后会恢复为 low。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'level': {
                    'type': 'string',
                    'enum': ['off', 'low', 'medium', 'high'],
                    'description': '可选，要设置的思考强度；不传时仅查看当前等级',
                },
            },
            'required': [],
        },
    },
    'set_session_mode': {
        'name': 'set_session_mode',
        'description': (
            '查看或设置当前会话的工作模式。'
            'mode 可选值为 chat、code；不传 mode 时只返回当前模式。'
            'chat 模式为纯聊天模式：只做盯群、抠情报线索上报、以及用已挂载知识库答问，'
            '工具列表大幅精简，没有任务派发、搜索、记忆、日志等工具，并额外强化人设说话风格；'
            'code 模式为完整模式，包含任务派发、搜索、文件操作等全部工具。'
            '默认是 chat。要干活（派 agent、查资料、跑命令）时先切 code，活干完切回 chat 省 token。'
            '默认只作用于当前会话；若你是主 AI，可传 target_scope_type/target_scope_id 直接切别的会话，'
            '子AI 请求干活权限时你就用这个给它开 code，事后记得收回。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'mode': {
                    'type': 'string',
                    'enum': ['chat', 'code'],
                    'description': '可选，要设置的工作模式；不传时仅查看当前模式',
                },
                'target_scope_type': {
                    'type': 'string',
                    'description': '可选，仅主 AI 可用：目标会话类型（group/private），切别的会话时使用',
                },
                'target_scope_id': {
                    'type': 'string',
                    'description': '可选，仅主 AI 可用：目标会话 ID，切别的会话时使用',
                },
            },
            'required': [],
        },
    },
    'set_trigger_rate': {
        'name': 'set_trigger_rate',
        'description': (
            '查看或设置随机触发概率。rate 范围只能在 0 到 0.30 之间，支持设置为 0；不传 rate 时只查看当前值。'
            '分级 AI 默认触发率为 0（不随机触发群聊），由你按需调高；设置会持久化到会话画像。'
            '默认只作用于当前会话；若你是主 AI，可传 target_scope_type/target_scope_id 调控其他会话。'
            '若你是主 AI 且想改的是“所有会话 + 以后新建的会话”，必须传 target_scope_type="global"：'
            '它会同步全局默认值、刷新所有现有会话并写入 config.yaml，重启后仍然生效。'
            '只逐个会话设置的话，新加的群/新私聊仍会按旧的全局默认值播种，看起来就像“重启后设置丢了”。'
            '注意：私聊默认本来就会触发，这个值主要影响群聊里的随机触发。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'rate': {
                    'type': 'number',
                    'description': '可选，要设置的随机触发概率，范围 0~0.30，可为 0',
                },
                'target_scope_type': {
                    'type': 'string',
                    'description': (
                        '可选，仅主 AI 可用：group/private 表示调控指定的某个会话（需配 target_scope_id）；'
                        'global 表示改全局默认值（同步所有现有会话并写入 config.yaml，重启后保留），此时不需要 target_scope_id'
                    ),
                },
                'target_scope_id': {
                    'type': 'string',
                    'description': '可选，仅主 AI 可用：目标会话 ID，调控某个具体会话时使用；target_scope_type=global 时不需要',
                },
            },
            'required': [],
        },
    },
    'view_image': {
        'name': 'view_image',
        'description': (
            '查看/解析图片，返回该图片的文字描述。'
            '只有当你确实需要看懂图片内容才有意义时才调用（比如别人发图问你、图里有你需要理解的文字/梗/信息）；'
            '纯表情、刷屏、跟你无关的图不用看。'
            '如果传 message_ref（上下文里形如 [#A1B2] 的四位短ID），就查看那条历史消息里的图片；'
            '不传 message_ref 时，默认查看本次触发消息里的图片。'
            '如果你觉得图片还没有解析出来，你可以先不回答或保持沉默，等待解析结果返回后再回复。'
            '如果系统已经把图片解析结果作为上下文给你了，就直接把它当可信上下文使用，不必重复看图。'
            'index 从 1 开始，表示目标消息里的第几张图片；不传时默认第 1 张。'
            '可选 question 用来指定你想重点看什么（比如"图里的文字是什么""这个人在做什么"），不传则做通用描述。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'message_ref': {'type': 'string', 'description': '可选，要查看的历史消息短ID（四位字母数字，如 A1B2）'},
                'index': {'type': 'integer', 'description': '要查看目标消息里的第几张图片，从 1 开始，默认 1'},
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
            '如果某个表情还没备注，先用 view_sticker 看清它长什么样，再用 annotate_sticker 打备注（描述这个表情是什么、适合什么场景），方便以后凭记忆挑选。'
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
            '你看不到表情的图像内容，所以打备注前应先用 view_sticker 看清这个表情画的是什么，再据此写备注，避免凭空乱标。'
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
    'view_sticker': {
        'name': 'view_sticker',
        'description': (
            '查看/解析你自己收藏的某个表情包长什么样，返回该表情的文字描述。'
            'index 从 1 开始，对应 list_stickers 列出的序号。'
            '这是你给表情打备注前的关键一步：你看不到收藏表情的图像内容，只有先用 view_sticker 看清它画的是什么、'
            '表达什么情绪、有没有文字/梗，才能用 annotate_sticker 打出准确的备注。'
            '给还没备注、或备注不准的表情打标时，先调用它看图。'
            '可选 question 用来指定重点看什么，不传则做通用描述。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'index': {'type': 'integer', 'description': '要查看的表情序号，从 1 开始（来自 list_stickers）'},
                'question': {'type': 'string', 'description': '可选，你想重点了解这个表情的什么内容'},
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
    'send_voice': {
        'name': 'send_voice',
        'description': (
            '把一段文本直接转换成语音并发送到当前会话。'
            '运行时会先调用 TTS 生成音频文件，再自动转成语音消息发送；'
            '你不需要也不应该再传本地文件路径。'
            '可选 emotion 用于指定情感；当前满穗网关支持 default、fear、narration、pain、angry。'
            '可选 speaker_id 仍保留兼容；旧链路可以继续传音色/说话人 ID。'
            '在当前本地满穗接口里，如果没传 emotion，也可以继续用 speaker_id 传 1、mansui、满穗、sui_best 这类旧别名。'
            '可选 speed 和 volume 用于微调语速与音量。'
            '发送成功后会返回确认信息。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'text': {'type': 'string', 'description': '要转成语音并发送的文本内容'},
                'emotion': {
                    'type': 'string',
                    'enum': ['default', 'fear', 'narration', 'pain', 'angry'],
                    'description': '可选，指定情感；当前满穗网关支持 default、fear、narration、pain、angry',
                },
                'speaker_id': {'type': 'string', 'description': '可选，指定音色/说话人 ID；不传则使用默认配置'},
                'speed': {'type': 'number', 'description': '可选，语速，默认 1.0'},
                'volume': {'type': 'number', 'description': '可选，音量微调，默认 0.0'},
            },
            'required': ['text'],
        },
    },
    'send_file': {
        'name': 'send_file',
        'description': (
            '把当前 LiveAi 进程所在机器上的一个本地文件通过 QQ 发送给当前会话（私聊或群聊）。'
            'path 必须是该机器上的绝对路径（如 /my/pro/bot/LiveAi/data/files/report.pdf）；'
            '运行时会先读取文件内容并编码上传给 NapCat，不依赖 NapCat 直接访问这个路径，'
            '因此适用于 NapCat 与 LiveAi 分机部署的场景。'
            '可选 name 指定对方看到的显示文件名（含后缀），不传则取路径末尾的文件名。'
            '发送成功后返回确认信息。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': '当前 LiveAi 进程所在机器上文件的绝对路径'},
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
            '如果这条只是确认消息、你还需要继续干活（例如调用 create_task / create_tasker / notify_master '
            '创建任务，或调用查询类工具推进续期/续跑等工作），请传 continue_work=true，'
            '发送后本回合会保留后续轮次，你可以继续调用这些工具，不要先发消息后沉默让用户干等。'
            '如果这条消息就是最终答复、没有其他后续操作，continue_work 传 false 或不传（默认），发送后立即结束本回合。'
            '如果必须回复上下文里某条具体的、非紧挨着的跨行消息，可传 reply_to_id，值为消息前面的四位短ID（如 A1B2）；'
            '私聊及正常连续对话时非必要请勿使用引用回复；群聊里也只有在消息较多、必须明确指向某条历史消息时再用。'
            '群聊多人聊天时，不要默认每条消息都是对你说的：先判断发言者在对谁说话（@了谁、引用了谁、上一句接的是谁），'
            '只回应明确指向你、或你接得上且自然的对话，不要盲目应答或替别人抢答。'
            '运行时会自动转成 CQ reply。'
            '发送成功后会返回 message_id 和短ID，如果内容过时需要撤回可以调用 recall_message。'
            '真要说话就调用 send_message；如果判断现在不该回，就调用 stay_silent，二者二选一。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'content': {
                    'type': 'string',
                    'description': '要发送的消息内容；可以包含 <thinking>...</thinking> 思考区，发送前会自动移除；换行会被拆成多条独立消息分别发送，用换行分隔 1 到 3 条短句',
                },
                'continue_work': {
                    'type': 'boolean',
                    'description': (
                        '发送后是否继续本回合执行后续操作。'
                        '若这条只是确认消息、你还要继续调用工具干活（如 create_task / create_tasker / '
                        'notify_master / 查询类工具推进续期等），请传 true，系统会保留后续轮次供你继续；'
                        '若这就是最终答复、没有其他后续操作，请传 false 或不传（默认），系统发送后立即结束本回合，避免多余轮次。'
                    ),
                },
                'reply_to_id': {
                    'type': 'string',
                    'description': '可选，要回复的消息短ID（四位字母数字，如 A1B2）',
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
            '或说了反而尴尬）时调用它。图片还没解析出来、你需要先等等结果时，也可以先保持沉默。'
            '注意：不要用它来“假装沉默却把想说的话写在别处”——真要说话就调用 send_message，'
            '真不想说才调用 stay_silent。二者只能选其一。'
        ),
        'input_schema': {'type': 'object', 'properties': {}, 'required': []},
    },
    'recall_message': {
        'name': 'recall_message',
        'description': (
            '撤回你之前用 send_message 发出的一条消息。'
            '优先传 message_ref（send_message 返回的四位短ID），也兼容旧的 message_id。'
            '只在内容确实过时、不合适或发错时使用；如果生成过程中又收到新消息，导致刚才的话不合时宜了，可以考虑撤回。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'message_ref': {'type': 'string', 'description': '优先使用，要撤回的消息短ID（四位字母数字，如 A1B2）'},
                'message_id': {'type': 'string', 'description': '兼容旧参数：要撤回的真实 message_id'},
            },
            'required': [],
        },
    },
    'remember': {
        'name': 'remember',
        'description': '快速记一条 AI 工具备忘（与 memory_add 等价的简写），本回合结束时写入。适合顺手记下值得以后回忆的事实、约定或线索。',
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
            '遇到不知道、不清楚、跨会话信息可能不一致、事实可能过期、自己没有权限查证、或工具结果看不懂的情况，也应优先找主AI同步，不要自己硬猜。\n'
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
            '请求主AI 派发 agent/tasker、查代码、跨会话协作等任务时，把完整背景一次写清：目标、背景、相关文件/会话、约束、期望产出，方便主AI 转达给 agent 时不丢信息。'
            '平时发现可沉淀的人物情报（省份、职业、爱好、关系态度等）也应主动上报给主AI统一归档。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'content': {'type': 'string', 'description': '要上报给主AI的内容，或上述 JSON 格式的协调请求；请求派发 agent/tasker 或转达任务时务必带全背景'},
            },
            'required': ['content'],
        },
    },
    'create_task': {
        'name': 'create_task',
        'description': (
            '创建通用后台任务。常用 kind：set_alarm（定闹钟/提醒）、image_describe（图片解析）、'
            'delegate_to_child（委派其他会话）、message_scope（向指定会话发消息）。'
            '一次性代码/资料后台执行请优先使用 create_tasker；为兼容旧调用，kind=dev_agent 或 kind=tasker 仍会创建 tasker。'
            '如果用户正在等你回复，而任务又需要时间，先用 send_message 解释一下，再建任务。'
            '定闹钟时建议 kind=set_alarm，payload 优先写成 JSON，包含 due_at 或 time_expression、note、scope_type、scope_id、requester_qq。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'kind': {'type': 'string', 'description': '任务类型；tasker 与旧 dev_agent 均表示一次性 tasker'},
                'payload': {'type': 'string', 'description': '任务参数，建议 JSON 字符串'},
            },
            'required': ['kind', 'payload'],
        },
    },
    'create_tasker': {
        'name': 'create_tasker',
        'description': (
            '创建一个一次性后台 tasker，用于独立执行代码修改、项目排查或资料检索，完成汇报后结束。'
            'tasker 不占用当前对话上下文，可读写本地项目代码、执行 shell，并按授权访问 GitHub。'
            '它不是常驻 agent：不能多轮持续待命；需要长期跟进、反复补充要求时应使用 create_agent。'
            '适合查 GitHub 某项目实现、修改本地 bot 项目代码、找技术参考、对比别的仓库实现。'
            'task 必须提供全量情报：任务目标、背景、相关文件路径、已知约束、已尝试过或已排除的方案、期望产出与验收标准，一次性写全，不要让 tasker 靠猜补信息；涉及仓库时带上 owner/repo。'
            '如果这事需要时间，正确流程通常是先用 send_message 告知用户，再在后续轮次里调用 create_tasker。'
            '旧 create_task(kind="dev_agent") / create_task(kind="tasker") 调用仍兼容。'
            '注意：这是高权限工具，只有当前请求者是号主本人时才应创建。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'task': {'type': 'string', 'description': '交给 tasker 的一次性任务描述，必须包含全量情报（目标/背景/相关文件/约束/已尝试/期望产出/验收标准），宁可多写不可少写；tasker 缺信息会在汇报里列明缺口请求补充'},
                'github_repo': {'type': 'string', 'description': '可选，优先参考或操作的 GitHub 仓库，格式 owner/repo'},
                'payload': {'type': 'string', 'description': '兼容字段：JSON 字符串或自然语言任务描述；新调用优先使用 task'},
            },
            'required': ['task'],
        },
    },
    'list_tasks': {
        'name': 'list_tasks',
        'description': (
            '查询后台任务列表。可以按 kind（任务类型）、status（状态：pending/running/done）筛选。'
            '主AI可以查看所有任务；子AI可以查看自己创建的任务。'
            '用于了解 tasker、闹钟、跨会话协作等后台任务的执行状态和结果。'
            '当用户追问“好了吗”“进度呢”时，先查它，不要凭印象回答。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'kind': {'type': 'string', 'description': '可选，按任务类型筛选（如 tasker、set_alarm；旧 dev_agent 也兼容）'},
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
            '当你已经知道 task_id，或用户正在追问某个具体任务时优先用它，不要凭感觉猜任务有没有完成。'
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
            '下载聊天消息中出现的文件并保存到本地，返回保存路径，供后续 tasker 或常驻 agent 读取分析。'
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
            '除了闹钟/提醒，也可以在创建常驻 agent 后配合它做定时检查、催进度、定期汇报。'
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
            'task_id 用 list_recurring_tasks 返回的 ID 或其唯一前缀（如 abcdef12）。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'task_id': {'type': 'string', 'description': '任务ID，支持唯一前缀'},
                'schedule': {'type': 'string', 'description': '可选，新的 cron 表达式'},
                'instruction': {'type': 'string', 'description': '可选，新的任务描述'},
                'enabled': {'type': 'boolean', 'description': '可选，true=启用，false=暂停'},
            },
            'required': ['task_id'],
        },
    },
    'delete_recurring_task': {
        'name': 'delete_recurring_task',
        'description': (
            '永久删除循环定时任务。'
            'task_id 用 list_recurring_tasks 返回的 ID 或其唯一前缀（如 abcdef12）。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'task_id': {'type': 'string', 'description': '任务ID，支持唯一前缀'},
            },
            'required': ['task_id'],
        },
    },
    'create_agent': {
        'name': 'create_agent',
        'description': (
            '创建一个常驻后台 agent 并立即让它开工，返回 agent_id。'
            '与一次性 tasker 不同，常驻 agent 会持续存在、可多轮双向沟通：'
            '它能读写本地项目代码、执行 shell、只读查阅或（token 权限允许时）改动 GitHub 仓库；'
            '干完一段会挂起待命，可以随时用 send_to_agent 追加指令、用 peek_agent 查进度、'
            '用 destroy_agent 结束它。适合需要长期跟进、分阶段推进或反复交互的后台工作。'
            '创建后系统会自动开启巡检定时器，每 5 分钟把本会话所有 agent 的进度合并推给你一次；'
            '同一会话的多个 agent 共用这一个定时器，全部结束后自动清理，你不需要自己 create_recurring_task。'
            'instruction 必须提供全量情报：任务目标、背景、相关文件路径、已知约束、已尝试过或已排除的方案、期望产出与验收标准、需要的环境与权限，一次性写全，不要让 agent 靠猜补信息；涉及仓库时带上 owner/repo。'
            '可选 cwd 指定工作目录：/ 表示仓库根目录，~ 表示项目目录，也可写成 /core 或 ~/pack 这种项目内路径。'
            '可选 read_only=true 表示只读模式：禁止修改本地文件、禁止写 GitHub、禁止执行可能改动环境的 shell。'
            '简单、一次性的活优先用 create_tasker；只有需要长期跟进或中途持续补充要求时再用它。'
            '注意：这是高权限工具，只有当前请求者是号主本人时才应创建。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'instruction': {'type': 'string', 'description': '交给该 agent 的任务描述，必须包含全量情报（目标/背景/相关文件/约束/已尝试/期望产出/验收标准），宁可多写不可少写；agent 凭它开工，缺信息会主动来问'},
                'cwd': {'type': 'string', 'description': '可选工作目录。/ 为仓库根目录，~ 为项目目录，也可写 /subdir 或 ~/subdir'},
                'read_only': {'type': 'boolean', 'description': '是否启用只读模式。true 时仅允许只读查阅，不允许写文件/写 GitHub/执行可能修改环境的 shell'},
            },
            'required': ['instruction'],
        },
    },
    'list_ssh_profiles': {
        'name': 'list_ssh_profiles',
        'description': (
            '列出当前可用的 SSH profile。'
            '创建 ssh_agent 前先用它查看 profile_id、远程目标和根目录，避免把主机信息或密钥路径直接写进聊天参数。'
            '要创建远程 ssh agent 时应先调用它，不要自己编 ssh_profile_id。'
        ),
        'input_schema': {'type': 'object', 'properties': {}, 'required': []},
    },
    'create_ssh_agent': {
        'name': 'create_ssh_agent',
        'description': (
            '创建一个运行在远程 SSH 服务器上的常驻后台 agent，返回 agent_id。'
            '它会复用常驻 agent 的多轮协作机制，但本地文件/本地 shell 相关工具会改为操作远程服务器上的根目录。'
            'ssh_profile_id 必须来自 list_ssh_profiles；认证信息由后台配置承载，不通过聊天参数传递。'
            '创建后系统会自动开启巡检定时器，每 5 分钟把本会话所有 agent 的进度合并推给你一次；'
            '同一会话的多个 agent 共用这一个定时器，全部结束后自动清理，你不需要自己 create_recurring_task。'
            '可选 cwd 指定远程工作目录：/ 表示该 profile 的远程根目录，~ 语义等同，也可写成 /subdir 或 ~/subdir。'
            '可选 read_only=true 表示只读模式：禁止修改远程文件、禁止写 GitHub、禁止执行可能改动环境的 shell。'
            'instruction 必须提供全量情报：任务目标、背景、相关文件/服务路径、已知约束、已尝试过或已排除的方案、期望产出与验收标准，一次性写全，不要让 agent 靠猜补信息；涉及远程环境差异（如镜像与生产不一致、行尾、权限）要主动说明。'
            '它适合需要长期跟进的远程运维/排查任务；简单一次性远程查看不要滥用。'
            '注意：这是高权限工具，只有当前请求者是号主本人时才应创建。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'instruction': {'type': 'string', 'description': '交给该 ssh agent 的任务描述，必须包含全量情报（目标/背景/相关文件或服务/约束/已尝试/期望产出），宁可多写不可少写；agent 凭它开工，缺信息会主动来问'},
                'ssh_profile_id': {'type': 'string', 'description': '目标 SSH profile 的 ID，来自 list_ssh_profiles'},
                'cwd': {'type': 'string', 'description': '可选远程工作目录。/ 为 profile 根目录，~ 语义等同，也可写 /subdir 或 ~/subdir'},
                'read_only': {'type': 'boolean', 'description': '是否启用只读模式。true 时仅允许只读查阅，不允许写远程文件/写 GitHub/执行可能修改环境的 shell'},
            },
            'required': ['instruction', 'ssh_profile_id'],
        },
    },
    'send_to_agent': {
        'name': 'send_to_agent',
        'description': (
            '给指定常驻 agent 发送一条消息/追加指令，唤醒它继续工作。'
            '用于在 agent 挂起待命、运行中或 review_required 阶段复核态补充要求、回答提问、调整方向。'
            '若处于 review_required，发送“继续”或纠偏指令都会保留现有上下文并重置本阶段轮次，不会重开 agent。'
            'agent_id 来自 create_agent 的返回或 list_agents。'
            '如有需要，也可同时更新该 agent 的工作目录 cwd 或只读开关 read_only。'
            '当 agent 通过内部系统通知来问你问题、要你拍板或汇报阶段进度时，必须用这个工具回它；你在普通文字里直接说，agent 是收不到的。'
            '回答 agent 的提问要完整、正面、直接拍板：缺什么补什么、纠偏说清楚改哪，不要只回半句话让它继续猜。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'agent_id': {'type': 'string', 'description': '目标 agent 的 ID'},
                'message': {'type': 'string', 'description': '要发给该 agent 的消息或指令内容；回答其提问时务必完整正面，一次性把信息补齐、把决定拍板，不要让它靠猜继续'},
                'cwd': {'type': 'string', 'description': '可选，顺便更新 agent 工作目录。/ 为仓库根目录，~ 为项目目录，也可写 /subdir 或 ~/subdir'},
                'read_only': {'type': 'boolean', 'description': '可选，顺便更新 agent 是否只读。true=只读，false=可写'},
            },
            'required': ['agent_id', 'message'],
        },
    },
    'peek_agent': {
        'name': 'peek_agent',
        'description': (
            '获取指定常驻 agent 当前的进度总结（由一个只读、无工具权限的总结 AI 生成），'
            '不会打断它正在进行的工作。用于了解 agent 干到哪一步、有没有卡住或风险。'
            '想知道进展但暂时不想打扰它时优先用这个。'
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
            '列出当前所有常驻 agent 及其状态（running/waiting/idle/review_required/error）、任务摘要、阶段轮次、消息数与时间。'
            'review_required 表示达到阶段轮次上限、上下文仍保留，需复核后用 send_to_agent 发送“继续”或纠偏指令；'
            'error 才表示真正运行异常。用于在创建/查看/继续/销毁 agent 前先掌握全局情况。'
            'waiting 表示它已经输出纯文本、正在等你答复；idle 表示干完待命，还没销毁。'
        ),
        'input_schema': {'type': 'object', 'properties': {}, 'required': []},
    },
    'destroy_agent': {
        'name': 'destroy_agent',
        'description': (
            '销毁指定常驻 agent：强制中断它的常驻循环并移除记录（会自动清理它的后台 shell 任务）。'
            'summarize=true 时会在销毁前先做一份总结（已完成的操作、可能遗留的隐患），随结果返回。'
            '确认某个 agent 不再需要时使用。用完记得销毁，回收资源，避免后台一直挂着。'
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
                'base_url': {'type': 'string', 'description': 'API 基础地址，必须自行包含版本路径（如 https://api.example.com/v1 或 /v2），系统不会自动补充版本'},
                'api_key': {'type': 'string', 'description': 'API 密钥'},
                'protocol': {'type': 'string', 'description': '接口协议；系统按协议拼接 /messages、/chat/completions 或 /responses，不附加 /v1', 'enum': ['anthropic', 'completions', 'responses']},
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
                'strategy': {'type': 'string', 'description': '轮询策略: fallback（失败后切换并持续停留）| fallback_reset（失败时递减，每轮请求从第一个开始）| random（每次随机）| roundrobin（每次轮询）', 'enum': ['fallback', 'fallback_reset', 'random', 'roundrobin']},
                'upstream': {'type': 'string', 'description': 'addmodel 时：上游名称'},
                'model_id': {'type': 'string', 'description': 'addmodel 时：模型 ID'},
                'model_index': {'type': 'integer', 'description': 'removemodel 时：要删除的模型序号（从0开始）'},
            },
            'required': ['action'],
        },
    },
    'manage_role': {
        'name': 'manage_role',
        'description': '管理角色-渠道绑定（三级架构第三级）。为 main/tiered/tiered_chat/tiered_exec/tiered_decision/agent/tasker/vision 各角色指定渠道；tiered_* 子渠道未配置时自动回退 tiered→main。旧 role=dev_agent 输入仍兼容。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'description': 'list | set', 'enum': ['list', 'set']},
                'role': {'type': 'string', 'description': 'set 时：角色名，可选 main / tiered / tiered_chat / tiered_exec / tiered_decision / agent / tasker / vision（旧 dev_agent 输入仍兼容）'},
                'channel': {'type': 'string', 'description': 'set 时：要绑定的渠道名称'},
            },
            'required': ['action'],
        },
    },
    'manage_ssh_profile': {
        'name': 'manage_ssh_profile',
        'description': '管理 SSH 服务器配置。支持查看、添加、修改、删除多个 SSH profile，供 create_ssh_agent 选择使用。仅管理员可用。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'description': 'list | add | update | remove', 'enum': ['list', 'add', 'update', 'remove']},
                'profile_id': {'type': 'string', 'description': 'SSH profile 的唯一 ID'},
                'target': {'type': 'string', 'description': 'SSH 目标，可写 user@host 或 ~/.ssh/config 里的 Host 别名'},
                'root_dir': {'type': 'string', 'description': '远程根目录，默认 ~'},
                'port': {'type': 'integer', 'description': 'SSH 端口，默认 22'},
                'identity_file': {'type': 'string', 'description': '可选私钥文件路径，留空则走系统默认 ssh 配置'},
                'shell': {'type': 'string', 'description': '远程 shell，默认 bash'},
                'strict_host_key_checking': {'type': 'boolean', 'description': '是否严格校验主机指纹，默认 true'},
            },
            'required': ['action'],
        },
    },
    'validate_ssh_profile': {
        'name': 'validate_ssh_profile',
        'description': '验证一个已配置 SSH profile 的连通性与根目录可达性。仅管理员可用。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'profile_id': {'type': 'string', 'description': '要验证的 SSH profile ID'},
            },
            'required': ['profile_id'],
        },
    },
    'query_logs': {
        'name': 'query_logs',
        'description': (
            '查询系统运行时日志。用于排查问题、了解常驻 agent/tasker/其他后台任务状态、检查 API 调用情况、'
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
    'qq_add_friend': {
        'name': 'qq_add_friend',
        'description': '主动添加 QQ 好友（高风险，仅主AI可用）。当前标准 NapCat/OneBot 11 通常不支持；协议端不支持时会明确报错且绝不伪报成功。',
        'input_schema': {'type': 'object', 'properties': {'user_id': {'type': 'integer', 'description': '目标 QQ 号'}, 'comment': {'type': 'string', 'description': '验证消息，可选'}}, 'required': ['user_id']},
    },
    'qq_list_friend_requests': {
        'name': 'qq_list_friend_requests',
        'description': '列出好友申请（高敏感，仅主AI可用）。返回本进程收到的 OneBot request 事件，以及 NapCat 可疑好友申请扩展接口结果；不会向普通会话暴露 flag。',
        'input_schema': {'type': 'object', 'properties': {'count': {'type': 'integer', 'description': '数量，1-100，默认50'}}, 'required': []},
    },
    'qq_approve_friend_request': {
        'name': 'qq_approve_friend_request',
        'description': '同意好友申请（高风险，仅主AI可用）。flag 必须来自申请事件/列表；成功仅以 OneBot status=ok 为准。',
        'input_schema': {'type': 'object', 'properties': {'flag': {'type': 'string', 'description': '申请 flag'}, 'remark': {'type': 'string', 'description': '好友备注，可选'}}, 'required': ['flag']},
    },
    'qq_reject_friend_request': {
        'name': 'qq_reject_friend_request',
        'description': '拒绝好友申请（高风险，仅主AI可用）。OneBot 好友拒绝动作不支持 reason。',
        'input_schema': {'type': 'object', 'properties': {'flag': {'type': 'string', 'description': '申请 flag'}}, 'required': ['flag']},
    },
    'qq_join_group': {
        'name': 'qq_join_group',
        'description': '主动申请加入 QQ 群（高风险，仅主AI可用）。当前标准 NapCat/OneBot 11 通常不支持；不支持时明确报错且不发送请求。',
        'input_schema': {'type': 'object', 'properties': {'group_id': {'type': 'integer', 'description': '目标群号'}, 'comment': {'type': 'string', 'description': '验证消息，可选'}}, 'required': ['group_id']},
    },
    'qq_list_group_requests': {
        'name': 'qq_list_group_requests',
        'description': '列出加群邀请/加群请求（高敏感，仅主AI可用），来源为 get_group_system_msg 和本进程 request 事件缓存。',
        'input_schema': {'type': 'object', 'properties': {'count': {'type': 'integer', 'description': '数量，1-100，默认50'}}, 'required': []},
    },
    'qq_approve_group_request': {
        'name': 'qq_approve_group_request',
        'description': '同意加群邀请或群请求（高风险，仅主AI可用）。必须提供 flag 与 sub_type（add/invite），二者应来自事件上下文。',
        'input_schema': {'type': 'object', 'properties': {'flag': {'type': 'string'}, 'sub_type': {'type': 'string', 'enum': ['add', 'invite']}}, 'required': ['flag', 'sub_type']},
    },
    'qq_reject_group_request': {
        'name': 'qq_reject_group_request',
        'description': '拒绝加群邀请或群请求（高风险，仅主AI可用）。必须提供 flag 与 sub_type；reason 可选，协议端可能忽略邀请拒绝理由。',
        'input_schema': {'type': 'object', 'properties': {'flag': {'type': 'string'}, 'sub_type': {'type': 'string', 'enum': ['add', 'invite']}, 'reason': {'type': 'string', 'description': '拒绝理由，可选'}}, 'required': ['flag', 'sub_type']},
    },
    'qq_sync_contacts': {
        'name': 'qq_sync_contacts',
        'description': '同步好友/群列表到联系人身份库（仅主AI可用）。从协议端拉取当前好友与群列表，刷新本机身份记录，返回好友数/群数与本次新增的联系人数；适合在新加好友、新入群后主动刷新认知。',
        'input_schema': {'type': 'object', 'properties': {}, 'required': []},
    },
    'validate_model_config': {
        'name': 'validate_model_config',
        'description': '对已配置的渠道或模型发起真实最小生成请求，验证可用性。不接受自定义 URL/key/prompt，结果不含凭据。仅管理员可用。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'target_type': {'type': 'string', 'enum': ['channel', 'model'], 'description': '验证整个渠道还是单个模型'},
                'channel': {'type': 'string', 'description': '已配置的渠道名'},
                'upstream': {'type': 'string', 'description': '验证单模型时必填：上游名'},
                'model_id': {'type': 'string', 'description': '验证单模型时必填：模型 ID'},
            },
            'required': ['target_type', 'channel'],
        },
    },
    'switch_agent_channel': {
        'name': 'switch_agent_channel',
        'description': '将指定常驻 agent 切换到已配置的渠道。不会创建 agent；正在进行的请求仍走旧渠道，从下一次模型调用生效。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'agent_id': {'type': 'string', 'description': 'agent ID'},
                'channel': {'type': 'string', 'description': '目标已配置渠道名'},
            },
            'required': ['agent_id', 'channel'],
        },
    },
    'relation_lookup': {
        'name': 'relation_lookup',
        'description': '按 QQ号或昵称查询关系网中的人物档案，返回昵称/省份/印象/属性情报/事实/好感度/备注。需要先理解对方是谁、和谁什么关系、有没有已知偏好时可先查它；落库前也应先查，做增量更新避免重复。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'QQ号 或 昵称（支持模糊匹配）'},
            },
            'required': ['query'],
        },
    },
    'relation_list': {
        'name': 'relation_list',
        'description': '列出关系网概览。kind=user 列人物档案，kind=scope 列群聊/私聊会话关系。适合先快速扫一眼当前已有谁、哪些会话有沉淀，再决定是否进一步 relation_lookup。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'kind': {'type': 'string', 'enum': ['user', 'scope'], 'description': "'user'(默认) 或 'scope'"},
                'limit': {'type': 'integer', 'description': '返回条数，默认 20'},
            },
            'required': [],
        },
    },
    'relation_update_user': {
        'name': 'relation_update_user',
        'description': '写入/更新一个人物的结构化情报（仅主AI可用）。只更新传入字段。province=省份，impression=印象，attributes=任意键值情报（如 职业/性别/爱好/年龄段），affinity=好感度，admin_note=备注。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'user_id': {'type': 'string', 'description': '目标 QQ号'},
                'province': {'type': 'string', 'description': '省份/地域'},
                'impression': {'type': 'string', 'description': '对该用户的印象（自由文本）'},
                'attributes': {'type': 'object', 'description': '任意键值情报，如 {"职业":"程序员","爱好":"钓鱼"}'},
                'affinity': {'type': 'number', 'description': '好感度数值'},
                'admin_note': {'type': 'string', 'description': '备注'},
            },
            'required': ['user_id'],
        },
    },
    'relation_add_fact': {
        'name': 'relation_add_fact',
        'description': '给某人物追加一条事实/情报线索（仅主AI可用）。用于记录零散的、非结构化的情报。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'user_id': {'type': 'string', 'description': '目标 QQ号'},
                'fact': {'type': 'string', 'description': '要记录的事实/情报'},
            },
            'required': ['user_id', 'fact'],
        },
    },
    'manage_knowledge_base': {
        'name': 'manage_knowledge_base',
        'description': '管理知识库及其挂载。支持列出/创建/修改/删除知识库，查看和维护条目，以及把知识库挂载到指定会话分身上下文。仅主AI或管理员授权会话可用。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'action': {
                    'type': 'string',
                    'enum': ['list_bases', 'create_base', 'update_base', 'delete_base', 'list_entries', 'add_entry', 'update_entry', 'delete_entry', 'list_mounts', 'mount_base', 'unmount_base'],
                    'description': '要执行的知识库操作',
                },
                'kb_id': {'type': 'string', 'description': '知识库 ID。除 list_bases/create_base 外大多数操作都需要'},
                'name': {'type': 'string', 'description': '知识库名称。create_base/update_base 时使用'},
                'description': {'type': 'string', 'description': '知识库描述。create_base/update_base 时使用'},
                'entry_id': {'type': 'string', 'description': '知识条目 ID。update_entry/delete_entry 时使用'},
                'content': {'type': 'string', 'description': '知识内容。add_entry/update_entry 时使用'},
                'target_scope_type': {'type': 'string', 'description': '挂载目标会话类型：private 或 group'},
                'target_scope_id': {'type': 'string', 'description': '挂载目标会话 ID'},
            },
            'required': ['action'],
        },
    },
    'request_knowledge_base_update': {
        'name': 'request_knowledge_base_update',
        'description': '向主AI发起知识库补充/修订建议。你只能提交纯文本意见，由主AI决定是否采纳、加到哪个知识库、怎么表述，并会回你批准或拒绝。适合上报值得长期沉淀的群规则、专用词汇、号主习惯、稳定设定；不要把无意义闲聊片段、短效情绪或没有依据的猜测塞进去。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'suggestion': {'type': 'string', 'description': '纯文本意见，说明建议新增/修改/删除什么知识以及理由'},
            },
            'required': ['suggestion'],
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
    include_qq_request_management: bool = False,
    include_relation_read: bool = False,
    include_relation_write: bool = False,
    include_knowledge_management: bool = False,
    include_knowledge_request: bool = False,
    cache_last: bool = True,
    immediate_mode: bool = False,
    chat_mode: bool = False,
) -> list[dict]:
    names: list[str] = []
    if chat_mode:
        include_memory = False
        allow_search = False
        include_download_file = False
        allow_tasks = False
        allow_recurring_tasks = False
        include_remember = False
        # 号主私聊默认也是 chat 模式，配置/更新类工具保留，否则改配置得先切模式。
    if include_memory:
        names.extend(['memory_list', 'memory_get', 'memory_add', 'memory_update'])
    if allow_search:
        names.append('web_search')
    names.append('set_thinking_level')
    names.append('set_session_mode')
    names.append('set_trigger_rate')
    if not chat_mode:
        # 纯聊天不需要代码和运行日志工具，去掉省 token。
        names.extend(['find_in_project', 'list_local_files', 'read_local_file', 'query_logs'])
    if include_download_file:
        names.append('download_file')
    if include_remember:
        names.append('remember')
    if allow_notify_master:
        names.append('notify_master')
    if allow_tasks:
        names.extend(['create_task', 'create_tasker'])
        names.extend(['create_agent', 'create_ssh_agent', 'list_ssh_profiles', 'send_to_agent', 'peek_agent', 'list_agents', 'destroy_agent'])
        names.append('switch_agent_channel')
    if allow_recurring_tasks:
        names.extend(['create_recurring_task', 'list_recurring_tasks', 'update_recurring_task', 'delete_recurring_task'])
    if allow_update_tools:
        names.extend(['check_github_version', 'execute_update'])
    if allow_config_tools:
        names.extend(['manage_upstream', 'manage_channel', 'manage_role', 'manage_ssh_profile', 'validate_model_config', 'validate_ssh_profile'])
    if include_group_management:
        names.append('manage_mute')
    if include_qq_request_management:
        names.extend([
            'qq_add_friend', 'qq_list_friend_requests', 'qq_approve_friend_request', 'qq_reject_friend_request',
            'qq_join_group', 'qq_list_group_requests', 'qq_approve_group_request', 'qq_reject_group_request',
        ])
    if include_relation_read:
        names.extend(['relation_lookup', 'relation_list'])
    if include_relation_write:
        names.extend(['relation_update_user', 'relation_add_fact'])
    if include_knowledge_management:
        names.append('manage_knowledge_base')
    if include_knowledge_request:
        names.append('request_knowledge_base_update')
    if include_message:
        names.append('send_message')
    if include_message and immediate_mode:
        names.append('recall_message')
        names.append('stay_silent')
        names.extend(['view_image', 'list_stickers', 'annotate_sticker', 'send_sticker', 'view_sticker', 'send_local_image', 'send_voice', 'send_file'])
    tools = [copy.deepcopy(_TOOL_DEFINITIONS[name]) for name in names]
    if tools and cache_last:
        tools[-1]['cache_control'] = {'type': 'ephemeral'}
    return tools


def code_mode_tool_names() -> set[str]:
    """只有 code 模式才有的工具名；用来判断会话是不是还需要待在 code 模式。"""
    kwargs = dict(
        include_relation_read=True,
        include_relation_write=True,
        include_knowledge_request=True,
        include_knowledge_management=True,
        allow_config_tools=True,
        allow_update_tools=True,
        include_group_management=True,
        include_qq_request_management=True,
        immediate_mode=True,
    )
    full = {tool['name'] for tool in build_tools(chat_mode=False, **kwargs)}
    chat = {tool['name'] for tool in build_tools(chat_mode=True, **kwargs)}
    # set_session_mode 本身不算干活，否则一调用就把计数清零，永远等不到下一次提示。
    return (full - chat) - {'set_session_mode'}
