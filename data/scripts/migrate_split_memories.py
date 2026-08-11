"""方案B 迁移脚本：把单文件 ai_state.json 的 memories 按 scope 拆分成独立文件，
并对存量 turn_logs 按新策略瘦身。

用法：
    python scripts/migrate_split_memories.py --dry-run   # 只校验+打印报告，不动真实文件
    python scripts/migrate_split_memories.py             # 正式迁移（会自动先备份）

设计要点：
- 主文件保留 agents/tasks/relations/settings/knowledge_base 等小数据，去掉 memories。
- 每个 scope 的 memory 存成 data/msgs/memories/{scope_type}__{scope_id}.json。
- 存量 turn_logs 迁移时按 slim_turn_log 瘦身，并只保留最近 TURN_LOG_LIMIT 条。
- 正式迁移前自动备份，所有写入先写 .new/.tmp 再原子 replace。
- 完整性校验：scope 数、各列表条数、顶层键条数逐一比对，任一不过即中止。
- 幂等：主文件写入 _schema_version=2；已是 v2 则跳过。
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

# 允许脚本直接运行时找到项目包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pack.scoped_memory_store import scope_filename  # noqa: E402
from core.turn_log_slim import slim_turn_log, TURN_LOG_LIMIT  # noqa: E402

SCHEMA_VERSION = 2
MEMORY_SUBKEYS = ('messages', 'notes', 'tool_logs', 'turn_logs')


def _load_json(path: Path) -> dict:
    raw = path.read_text(encoding='utf-8').strip() or '{}'
    return json.loads(raw)


def _dump_compact(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))


def migrate(state_path: Path, dry_run: bool) -> int:
    if not state_path.exists():
        print(f'[ERR] 状态文件不存在: {state_path}')
        return 2

    payload = _load_json(state_path)

    if payload.get('_schema_version') == SCHEMA_VERSION and 'memories' not in payload:
        print('[SKIP] 已是 schema v2 且无 memories，无需迁移。')
        return 0

    memories = payload.get('memories') or {}
    if not memories:
        print('[WARN] 主文件没有 memories 块，可能已迁移或本就为空。')

    mem_dir = state_path.parent / 'memories'

    print('=' * 60)
    print(f'状态文件: {state_path}  ({state_path.stat().st_size} bytes)')
    print(f'memories 目录: {mem_dir}')
    print(f'模式: {"DRY-RUN（不写真实文件）" if dry_run else "正式迁移"}')
    print(f'scope 数量: {len(memories)}')
    print(f'turn_logs 保留条数: {TURN_LOG_LIMIT}')
    print('=' * 60)

    # 逐 scope 构建瘦身后的 memory，并生成校验报告
    per_scope_files: dict[str, dict] = {}
    index_map: dict[str, str] = {}
    report_rows = []
    total_before = 0
    total_after = 0
    ok = True

    for scope_key, mem in memories.items():
        mem = mem or {}
        fname = scope_filename(scope_key)
        if fname in index_map.values():
            print(f'[ERR] 文件名碰撞: {scope_key} -> {fname}')
            ok = False
        index_map[scope_key] = fname

        new_mem = {}
        row = {'scope': scope_key, 'file': fname}
        for sub in MEMORY_SUBKEYS:
            items = mem.get(sub) or []
            row[f'{sub}_before'] = len(items)
            if sub == 'turn_logs':
                slimmed = [slim_turn_log(x) for x in items][-TURN_LOG_LIMIT:]
                new_mem[sub] = slimmed
                row['turn_logs_after'] = len(slimmed)
            else:
                new_mem[sub] = items
        # 保留 memory 里除四大子键外的其它字段（防御未知字段）
        for k, v in mem.items():
            if k not in MEMORY_SUBKEYS:
                new_mem.setdefault(k, v)

        before_bytes = len(_dump_compact(mem).encode('utf-8'))
        after_bytes = len(_dump_compact(new_mem).encode('utf-8'))
        total_before += before_bytes
        total_after += after_bytes
        row['KB_before'] = before_bytes // 1024
        row['KB_after'] = after_bytes // 1024
        report_rows.append(row)
        per_scope_files[scope_key] = new_mem

    # 校验：非 turn_logs 的条数必须完全一致
    print('\n逐 scope 校验报告：')
    print(f'{"scope":<24}{"msgs":>7}{"notes":>7}{"tools":>7}{"turn(前→后)":>14}{"KB(前→后)":>16}')
    for row in report_rows:
        turn_str = f'{row["turn_logs_before"]}→{row["turn_logs_after"]}'
        kb_str = f'{row["KB_before"]}→{row["KB_after"]}'
        print(f'{row["scope"]:<24}{row["messages_before"]:>7}{row["notes_before"]:>7}'
              f'{row["tool_logs_before"]:>7}{turn_str:>14}{kb_str:>16}')

        # messages/notes/tool_logs 条数应无损
        src = per_scope_files[row['scope']]
        for sub in ('messages', 'notes', 'tool_logs'):
            if len(src[sub]) != row[f'{sub}_before']:
                print(f'  [ERR] {row["scope"]} {sub} 条数不一致: '
                      f'{row[f"{sub}_before"]} -> {len(src[sub])}')
                ok = False

    print('\n汇总：')
    print(f'  scope 总数: {len(memories)}')
    print(f'  memories 总体积: {total_before // 1024} KB → {total_after // 1024} KB '
          f'(减 {((total_before - total_after) / max(total_before,1) * 100):.1f}%)')

    # 主文件（去掉 memories）
    new_state = {k: v for k, v in payload.items() if k != 'memories'}
    new_state['_schema_version'] = SCHEMA_VERSION
    new_state_bytes = len(_dump_compact(new_state).encode('utf-8'))
    print(f'  新主文件体积: {new_state_bytes // 1024} KB（原 {state_path.stat().st_size // 1024} KB）')

    # 顶层小键条数比对（迁移前后应一致）
    for topk in ('agents', 'tasks', 'relations'):
        before = len(payload.get(topk) or {})
        after = len(new_state.get(topk) or {})
        flag = 'OK' if before == after else 'ERR'
        if before != after:
            ok = False
        print(f'  顶层[{topk}] {before} -> {after}  [{flag}]')

    print('=' * 60)
    if not ok:
        print('[ABORT] 校验未通过，未写入任何真实文件。请检查上面的 [ERR]。')
        return 1

    if dry_run:
        print('[DRY-RUN OK] 校验全部通过。未写入真实文件。')
        return 0

    # ===== 正式写入 =====
    # 1) 备份
    ts = time.strftime('%Y%m%d_%H%M%S')
    backup = state_path.with_name(state_path.name + f'.premigrate.{ts}')
    shutil.copy2(state_path, backup)
    print(f'[BACKUP] {backup} ({backup.stat().st_size} bytes)')

    # 2) 写 memories 目录
    mem_dir.mkdir(parents=True, exist_ok=True)
    for scope_key, new_mem in per_scope_files.items():
        fpath = mem_dir / index_map[scope_key]
        tmp = fpath.with_suffix('.json.tmp')
        tmp.write_text(_dump_compact(new_mem), encoding='utf-8')
        tmp.replace(fpath)
    idx_path = mem_dir / '_index.json'
    idx_tmp = idx_path.with_suffix('.json.tmp')
    idx_tmp.write_text(_dump_compact(index_map), encoding='utf-8')
    idx_tmp.replace(idx_path)
    print(f'[WRITE] {len(per_scope_files)} 个 scope 文件 + _index.json 已写入 {mem_dir}')

    # 3) 原子替换主文件
    new_path = state_path.with_suffix('.json.new')
    new_path.write_text(_dump_compact(new_state), encoding='utf-8')
    new_path.replace(state_path)
    print(f'[WRITE] 新主文件已写入 {state_path} ({state_path.stat().st_size} bytes)')

    print('[DONE] 迁移完成。')
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', default='data/msgs/ai_state.json',
                        help='ai_state.json 路径')
    parser.add_argument('--dry-run', action='store_true',
                        help='只校验并打印报告，不写任何真实文件')
    args = parser.parse_args()
    return migrate(Path(args.state), args.dry_run)


if __name__ == '__main__':
    raise SystemExit(main())
