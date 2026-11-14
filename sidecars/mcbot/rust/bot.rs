//! Headless NDJSON control surface for the local DeepLive sidecar.
//!
//! stdin/stdout carry small JSON messages only. Existing diagnostics stay on
//! stderr. No listener, account token, or remote control endpoint is added.
use crate::{Config, ConnectionPreferences, auth::AuthSession, live, session_probe};
use mc_protocol::Result;
use serde::{Deserialize, Serialize};
use std::{
    io::BufRead,
    path::PathBuf,
    sync::mpsc,
    time::Duration,
};

#[derive(Debug, Deserialize)]
struct Request {
    id: serde_json::Value,
    op: String,
    #[serde(default)]
    message: String,
    #[serde(default)]
    buttons: u8,
    #[serde(default)]
    look: [f32; 2],
    #[serde(default)]
    down: bool,
    #[serde(default)]
    slot: Option<u8>,
    #[serde(default)]
    since: u64,
    /// 背包/容器操作的槽位（界面槽号；容器开着时含箱子区），与快捷栏 `slot` 分开。
    #[serde(default)]
    inv_slot: Option<i16>,
    /// 鼠标键：0=左键 1=右键（swap 时 0..=8 是快捷栏编号）。
    #[serde(default)]
    button: u8,
    /// 丢弃整组。
    #[serde(default)]
    whole: bool,
    /// 本地世界扫描（op=scan）：目标方块名列表（可不带 minecraft: 前缀）。
    #[serde(default)]
    names: Vec<String>,
    #[serde(default)]
    radius: Option<i32>,
    #[serde(default)]
    dy_min: Option<i32>,
    #[serde(default)]
    dy_max: Option<i32>,
    #[serde(default)]
    limit: Option<usize>,
    /// 直发方块动作的目标方块坐标（dig_at / use_at）。
    #[serde(default)]
    pos: Option<[i32; 3]>,
    /// 命中面 0..=5（0=下 1=上 2=北 3=南 4=西 5=东），默认 1（上表面）。
    #[serde(default)]
    face: Option<u8>,
    /// 攻击目标实体 id（attack_entity）。
    #[serde(default)]
    entity: Option<i32>,
    /// 挖掘停留毫秒（dig_at），默认 1000。
    #[serde(default)]
    hold_ms: Option<u64>,
}

#[derive(Serialize)]
struct Item {
    slot: i32,
    name: String,
    count: i32,
}

#[derive(Serialize)]
struct Container {
    id: i32,
    menu: i32,
    rows: u8,
    title: String,
    slots: Vec<Item>,
}

#[derive(Serialize)]
struct Snapshot {
    protocol: i32,
    level_loading: bool,
    position: Option<[f64; 3]>,
    yaw: Option<f32>,
    pitch: Option<f32>,
    health: f32,
    food: i32,
    experience_level: i32,
    selected: u8,
    held: Option<Item>,
    inventory: Vec<Item>,
    /// 光标上挂着的物品（背包操作中间态；没有为 null）。
    carried: Option<Item>,
    /// 当前打开的容器（箱子/熔炉等）；没开为 null。
    container: Option<Container>,
    players: Vec<String>,
    chat_sequence: u64,
}

#[derive(Serialize)]
struct ChatEvent {
    sequence: u64,
    text: String,
}

fn item(slot: i32, value: &Option<mc_game::items::ItemView>) -> Option<Item> {
    value.as_ref().map(|item| Item {
        slot,
        name: item.name.clone(),
        count: item.count,
    })
}

fn snapshot(live: &live::LiveSession) -> Snapshot {
    let ui = live.ui();
    let inventory = &ui.inventory;
    let held = inventory
        .slots
        .get(usize::from(inventory.selected))
        .and_then(|value| item(i32::from(inventory.selected), value));
    let pose = live.pose();
    // container.chest 这类翻译键在无语言包时原样透出，对调用方已可读。
    let language = std::collections::BTreeMap::new();
    let container = (inventory.container_id != 0).then(|| Container {
        id: inventory.container_id,
        menu: inventory.container_menu,
        rows: inventory.container_rows,
        title: inventory.container_title.resolve(&language).to_owned(),
        slots: inventory
            .container_slots
            .iter()
            .enumerate()
            .filter_map(|(slot, value)| item(slot as i32, value))
            .collect(),
    });
    Snapshot {
        protocol: mc_protocol::VERSION,
        level_loading: ui.level_loading,
        position: pose.map(|pose| pose.current.position),
        yaw: pose.map(|pose| pose.current.yaw),
        pitch: pose.map(|pose| pose.current.pitch),
        health: inventory.health,
        food: inventory.food,
        experience_level: inventory.experience_level,
        selected: inventory.selected,
        held,
        inventory: inventory
            .storage
            .iter()
            .enumerate()
            .filter_map(|(slot, value)| item(slot as i32, value))
            .collect(),
        carried: item(-1, &inventory.carried),
        container,
        players: ui.tab_players.iter().map(|player| player.name.clone()).collect(),
        chat_sequence: ui.chat_sequence,
    }
}

fn events(live: &live::LiveSession, since: u64) -> (Vec<ChatEvent>, u64) {
    let ui = live.ui();
    (
        ui.chat_lines
            .iter()
            .filter(|line| line.sequence > since)
            .map(|line| ChatEvent {
                sequence: line.sequence,
                text: line.text.clone(),
            })
            .collect(),
        ui.chat_sequence,
    )
}

fn emit(value: serde_json::Value) -> Result<()> {
    println!("{}", serde_json::to_string(&value)?);
    Ok(())
}

fn reply(id: serde_json::Value, result: std::result::Result<serde_json::Value, String>) -> Result<()> {
    emit(match result {
        Ok(value) => serde_json::json!({"id": id, "ok": true, "result": value}),
        Err(error) => serde_json::json!({"id": id, "ok": false, "error": error}),
    })
}

fn execute(live: &live::LiveSession, request: &Request) -> std::result::Result<serde_json::Value, String> {
    match request.op.as_str() {
        "status" => Ok(serde_json::to_value(snapshot(live)).map_err(|e| e.to_string())?),
        "events" => {
            let (events, next) = events(live, request.since);
            Ok(serde_json::json!({"events": events, "next": next}))
        }
        "chat" => {
            if request.message.is_empty() || request.message.encode_utf16().count() > 256 {
                return Err("message must contain 1..=256 UTF-16 units".into());
            }
            if live.send_chat_units(request.message.encode_utf16().collect()) {
                Ok(serde_json::json!({"sent": true}))
            } else {
                Err("chat queue is closed or full".into())
            }
        }
        "command" => {
            let command = request.message.strip_prefix('/').unwrap_or(&request.message);
            if command.is_empty() || command.encode_utf16().count() > 32767 {
                return Err("invalid command length".into());
            }
            if live.send_unattended_command(command) {
                Ok(serde_json::json!({"sent": true}))
            } else {
                Err("command queue is closed or full".into())
            }
        }
        "move" => {
            // Mouse look uses the vanilla 0.15-degree raw unit, not degrees.
            if !request.look.iter().all(|value| value.is_finite() && value.abs() <= 2400.) {
                return Err("look delta must be finite and within ±2400 raw units".into());
            }
            live.input(request.buttons, request.look);
            Ok(serde_json::json!({"moving": request.buttons != 0}))
        }
        "stop" => {
            live.input(0, [0., 0.]);
            live.mouse_button(true, false, false);
            live.mouse_button(false, false, false);
            Ok(serde_json::json!({"stopped": true}))
        }
        "attack" | "use" => {
            live.mouse_button(request.op == "attack", request.down, request.down);
            Ok(serde_json::json!({"down": request.down}))
        }
        // 直发方块/实体动作：坐标精确、无需瞄准，绕开鼠标视角模拟层。
        "dig_at" => {
            let pos = request.pos.ok_or("pos [x,y,z] required")?;
            let face = request.face.unwrap_or(1);
            if face > 5 {
                return Err("face must be 0..=5".into());
            }
            let hold_ms = request.hold_ms.unwrap_or(1000).min(30_000);
            live.dig_at(pos, face, hold_ms).map_err(|e| e.to_string())?;
            Ok(serde_json::json!({"queued": true, "hold_ms": hold_ms}))
        }
        "use_at" => {
            let pos = request.pos.ok_or("pos [x,y,z] required")?;
            let face = request.face.unwrap_or(1);
            if face > 5 {
                return Err("face must be 0..=5".into());
            }
            live.use_at(pos, face).map_err(|e| e.to_string())?;
            Ok(serde_json::json!({"queued": true}))
        }
        "attack_entity" => {
            let entity = request.entity.ok_or("entity id required")?;
            live.attack_entity(entity).map_err(|e| e.to_string())?;
            Ok(serde_json::json!({"queued": true, "entity": entity}))
        }
        // 附近实体快照：按距离排序，可选半径过滤（默认全部，最多 256 个）。
        "entities" => {
            let center = live.pose().map(|pose| pose.current.position);
            let mut list: Vec<serde_json::Value> = live
                .entities()
                .into_iter()
                .map(|entity| {
                    let distance = center.map(|center| {
                        let dx = entity.position[0] - center[0];
                        let dy = entity.position[1] - center[1];
                        let dz = entity.position[2] - center[2];
                        (dx * dx + dy * dy + dz * dz).sqrt()
                    });
                    serde_json::json!({
                        "id": entity.id,
                        "kind": entity.kind,
                        "name": entity.name,
                        "position": entity.position,
                        "health": entity.health,
                        "max_health": entity.max_health,
                        "distance": distance,
                    })
                })
                .collect();
            list.sort_by(|a, b| {
                let da = a["distance"].as_f64().unwrap_or(f64::MAX);
                let db = b["distance"].as_f64().unwrap_or(f64::MAX);
                da.partial_cmp(&db).unwrap_or(std::cmp::Ordering::Equal)
            });
            if let Some(radius) = request.radius {
                let radius = f64::from(radius);
                list.retain(|entity| entity["distance"].as_f64().is_none_or(|d| d <= radius));
            }
            list.truncate(request.limit.unwrap_or(256).clamp(1, 256));
            Ok(serde_json::json!({"entities": list}))
        }
        "select" => {
            let slot = request.slot.filter(|slot| *slot < 9).ok_or("slot must be 0..=8")?;
            let selected = live.ui().inventory.selected;
            live.scroll_slot(i32::from(slot) - i32::from(selected));
            Ok(serde_json::json!({"selected": slot}))
        }
        "respawn" => {
            live.request_respawn();
            Ok(serde_json::json!({"requested": true}))
        }
        // 本地世界扫描：客户端区块缓存就在内存里，整盒查询毫秒级零网络——
        // 替代 RCON 一格一探（每格 ~10ms，半径 12 三层扫要两分钟）。
        // 按 block_id 匹配：同名方块的全部朝向/变体状态一次覆盖。
        "scan" => {
            let world = live.camera_world().ok_or("world not ready")?;
            let codec = world.block_state_codec().ok_or("block state codec not ready")?;
            let pose = live.pose().ok_or("position unknown")?;
            let [px, py, pz] = pose.current.position;
            let (cx, cy, cz) = (px.floor() as i32, py.floor() as i32, pz.floor() as i32);
            let radius = request.radius.unwrap_or(12).clamp(1, 64);
            let dy_min = request.dy_min.unwrap_or(-3);
            let dy_max = request.dy_max.unwrap_or(12);
            let limit = request.limit.unwrap_or(64).clamp(1, 512);
            if request.names.is_empty() {
                return Err("names required (要匹配的方块名列表)".into());
            }
            let mut targets: Vec<(u32, String)> = Vec::new();
            let mut unknown: Vec<String> = Vec::new();
            for raw in &request.names {
                let name = if raw.contains(':') { raw.clone() } else { format!("minecraft:{raw}") };
                match codec.named_state(&name, &[]).and_then(|state| codec.block_id(state)) {
                    Some(bid) => {
                        if !targets.iter().any(|(t, _)| *t == bid) {
                            targets.push((bid, name));
                        }
                    }
                    // 不认识的名单个跳过（JS 侧标签展开会猜新变种，别让整个扫描陪葬），
                    // 但全都不认识就是调用方写错了，报错。
                    None => unknown.push(name),
                }
            }
            if targets.is_empty() {
                return Err(format!("no known block names: {unknown:?}"));
            }
            let mut hits: Vec<(i64, [i32; 3], String)> = Vec::new();
            let mut scanned: u64 = 0;
            let mut unloaded: u64 = 0;
            for dx in -radius..=radius {
                for dz in -radius..=radius {
                    for dy in dy_min..=dy_max {
                        let (x, y, z) = (cx + dx, cy + dy, cz + dz);
                        match world.block_state(x, y, z) {
                            None => unloaded += 1,
                            Some(state) => {
                                scanned += 1;
                                let Some(bid) = codec.block_id(state) else { continue };
                                if let Some((_, name)) = targets.iter().find(|(t, _)| *t == bid) {
                                    let dist2 = i64::from(dx * dx) + i64::from(dz * dz);
                                    hits.push((dist2, [x, y, z], name.clone()));
                                }
                            }
                        }
                    }
                }
            }
            // 近者优先（近似螺旋序），截断到 limit
            hits.sort_by_key(|(dist2, _, _)| *dist2);
            hits.truncate(limit);
            let hits: Vec<serde_json::Value> = hits
                .into_iter()
                .map(|(_, pos, name)| serde_json::json!([pos[0], pos[1], pos[2], name]))
                .collect();
            Ok(serde_json::json!({
                "hits": hits,
                "scanned": scanned,
                "unloaded": unloaded,
                "center": [cx, cy, cz],
                "unknown": unknown,
            }))
        }
        // 本地导航网格：一次导出机器人周围的碰撞体素盒，JS 侧跑 A*。
        // 每格给「方块名 + 碰撞顶高（1/16 格精度）」，按 run-length 压平。
        // 顶高比布尔实体化更有用：台阶/楼梯/雪层是 4..15，可以直接走上去不该跳；
        // 满块才是 16。这样寻路能出人类脚步，而不是见坎就蹦。
        // 取代原来每 tick 2~3 次 `execute if block` 的盲探——那些 RCON 往返
        // 正是「走路一顿一顿」的根因。
        "nav" => {
            let (world, physics, _) = live.camera_environment().ok_or("world not ready")?;
            let codec = world.block_state_codec().ok_or("block state codec not ready")?;
            let pose = live.pose().ok_or("position unknown")?;
            let [px, py, pz] = pose.current.position;
            let (cx, cy, cz) = (px.floor() as i32, py.floor() as i32, pz.floor() as i32);
            let radius = request.radius.unwrap_or(16).clamp(1, 32);
            let dy_min = request.dy_min.unwrap_or(-8);
            let dy_max = request.dy_max.unwrap_or(8);
            if dy_max < dy_min {
                return Err("dy_max must be >= dy_min".into());
            }
            let (x0, y0, z0) = (cx - radius, cy + dy_min, cz - radius);
            let w = (radius * 2 + 1) as usize;
            let h = (dy_max - dy_min + 1) as usize;
            if w * w * h > 400_000 {
                return Err(format!("nav box too large ({w}x{h}x{w})"));
            }
            // 调色板：[名, 顶高]。0 号位固定是空气。
            let mut palette: Vec<(String, u8)> = vec![("minecraft:air".to_owned(), 0)];
            let mut palette_of: std::collections::HashMap<u32, u32> = std::collections::HashMap::new();
            let mut runs: Vec<[u32; 2]> = Vec::new();
            let mut unloaded: u64 = 0;
            let mut last: Option<u32> = None;
            for y in y0..y0 + h as i32 {
                for z in z0..z0 + w as i32 {
                    for x in x0..x0 + w as i32 {
                        let index = match world.block_state(x, y, z) {
                            None => {
                                unloaded += 1;
                                u32::MAX // 未加载：单独标成 MAX，JS 当不可通行
                            }
                            Some(state) => {
                                if let Some(&slot) = palette_of.get(&state) {
                                    slot
                                } else {
                                    let name = codec
                                        .name(state)
                                        .map(str::to_owned)
                                        .unwrap_or_else(|| format!("minecraft:unknown_{state}"));
                                    // 顶高取该状态全部碰撞盒的最大上沿，按 1/16 格取整。
                                    // context != 0 的方块（沙、脚手架一类）在 0 上下文下
                                    // 会退化成空盒，这里按它的静态形状兜底。
                                    let top = physics
                                        .states
                                        .get(state as usize)
                                        .map(|block| {
                                            let raw = block
                                                .boxes
                                                .iter()
                                                .map(|b| b.max.y)
                                                .fold(0.0_f64, f64::max);
                                            if raw <= 0.0 && block.context == 32 { 16.0 } else { raw }
                                        })
                                        .unwrap_or(0.0);
                                    let tall = (top * 16.0).round().clamp(0.0, 16.0) as u8;
                                    let slot = palette.len() as u32;
                                    palette.push((name, tall));
                                    palette_of.insert(state, slot);
                                    slot
                                }
                            }
                        };
                        match last {
                            Some(prev) if prev == index => {
                                if let Some(tail) = runs.last_mut() {
                                    tail[1] += 1;
                                }
                            }
                            _ => {
                                runs.push([index, 1]);
                                last = Some(index);
                            }
                        }
                    }
                }
            }
            let palette: Vec<serde_json::Value> = palette
                .into_iter()
                .map(|(name, tall)| serde_json::json!([name, tall]))
                .collect();
            Ok(serde_json::json!({
                "origin": [x0, y0, z0],
                "size": [w, h, w],
                "palette": palette,
                "runs": runs,
                // 0 号是空气；u32::MAX = 区块没加载（JS 当墙，别让它撞虚空）
                "unloaded": unloaded,
                "feet": [cx, cy, cz],
                "pos": [px, py, pz],
                "on_ground": pose.current.on_ground,
                "eye": pose.current.eye,
            }))
        }
        // ---- 背包/容器操作（复用 GUI 同款 LiveSession 接口，槽号即界面槽号）----
        "inv_click" => {
            let slot = request.inv_slot.ok_or("inv_slot required (界面槽号)")?;
            live.inventory_click(slot, request.button);
            Ok(serde_json::json!({"clicked": slot}))
        }
        "inv_quick_move" => {
            // shift+点击：容器区 ↔ 背包区间批量搬移
            let slot = request.inv_slot.ok_or("inv_slot required (界面槽号)")?;
            live.inventory_quick_move(slot, request.button);
            Ok(serde_json::json!({"quick_moved": slot}))
        }
        "inv_throw" => {
            let slot = request.inv_slot.ok_or("inv_slot required (界面槽号)")?;
            live.inventory_drop(slot, request.whole);
            Ok(serde_json::json!({"thrown": slot, "whole": request.whole}))
        }
        "inv_pickup_all" => {
            // 双击拾取：把散落的同类物品全收进光标
            let slot = request.inv_slot.ok_or("inv_slot required (界面槽号)")?;
            live.inventory_pickup_all(slot, request.button);
            Ok(serde_json::json!({"picked_up": slot}))
        }
        "inv_swap" => {
            // 数字键交换：槽位物品与快捷栏 button 格互换
            let slot = request.inv_slot.ok_or("inv_slot required (界面槽号)")?;
            if request.button > 8 {
                return Err("button must be 0..=8 for swap (快捷栏编号)".into());
            }
            live.inventory_swap(slot, request.button);
            Ok(serde_json::json!({"swapped": slot, "with_hotbar": request.button}))
        }
        "drop_hand" => {
            // 主手丢弃。注意不能走 drop_item()/swap_hands() 的按键旗——headless 没有
            // keybindings 上下文时 interaction 会把整个 input 重置成默认（实测空发）。
            // 等价窗口操作：对快捷栏槽 THROW（窗口 0 菜单槽 36+selected）。
            let selected = live.ui().inventory.selected;
            live.inventory_drop(i16::from(selected) + 36, request.whole);
            Ok(serde_json::json!({"dropped_held": true, "whole": request.whole}))
        }
        "swap_hands" => {
            // 等价 F 键：悬停副手槽(45)按当前快捷栏数字键 = Swap(45, selected)
            let selected = live.ui().inventory.selected;
            live.inventory_swap(45, selected);
            Ok(serde_json::json!({"swapped_hands": true}))
        }
        "container_close" => {
            live.inventory_open(false);
            Ok(serde_json::json!({"closed": true}))
        }
        other => Err(format!("unsupported bot operation: {other}")),
    }
}

struct Arguments {
    config: PathBuf,
    host: String,
    port: u16,
}

fn arguments(args: &[String]) -> std::result::Result<Arguments, &'static str> {
    let (mut config, mut host, mut port) = (None, None, None);
    let mut index = 0;
    while index < args.len() {
        let value = args.get(index + 1).ok_or("invalid_arguments")?;
        match args[index].as_str() {
            "--config" if config.is_none() && !value.is_empty() => config = Some(PathBuf::from(value)),
            "--connect" if host.is_none() && !value.is_empty() => host = Some(value.clone()),
            "--port" if port.is_none() => {
                port = Some(value.parse::<u16>().ok().filter(|port| *port > 0).ok_or("invalid_arguments")?)
            }
            _ => return Err("invalid_arguments"),
        }
        index += 2;
    }
    Ok(Arguments {
        config: config.ok_or("config_required")?,
        host: host.ok_or("connect_required")?,
        port: port.unwrap_or(25565),
    })
}

pub(crate) fn command(args: &[String]) -> Result<()> {
    let args = arguments(args)?;
    let bytes = crate::bounded_file(&args.config, 65536)?;
    let config: Config = serde_json::from_slice(&bytes)?;
    mc_game::runtime_data::initialize(config.runtime_data.as_deref())?;
    let preferences = ConnectionPreferences {
        chat_visibility: config.chat.visibility.wire(),
        chat_colors: config.chat.colors,
        particles: config.particles,
        view_distance: config.view_distance,
        main_hand_left: config.main_hand_left,
        skin_parts: config.skin_parts,
        session_file: config.session_file.clone(),
        assets_root: config.assets.clone(),
    };
    let connect = vec![
        "bot".into(),
        args.host,
        config.player_name.clone(),
        args.port.to_string(),
    ];
    let progress = session_probe::Progress::default();
    let (connection, session, packs, auth) = crate::negotiate_observed(
        &connect,
        Some(false),
        Some(preferences),
        None,
        None,
        true,
        None,
        AuthSession::join_server,
        Some(&progress),
    )?;
    let overlays = config.resource_pack.iter().map(PathBuf::from).collect::<Vec<_>>();
    let models = mc_assets::models::ModelLibrary::load_with_overlays(
        &config.assets,
        &mc_game::runtime_data::file("blockstates.tsv"),
        &overlays,
    )?;
    let mut options = session_probe::options(&config, auth, progress)?;
    options.initial_packs = packs;
    let mut live = live::LiveSession::start(connection, session, models, options)?;
    emit(serde_json::json!({"type": "ready", "ok": true, "protocol": mc_protocol::VERSION}))?;

    let (sender, receiver) = mpsc::channel::<std::result::Result<Request, String>>();
    std::thread::spawn(move || {
        let stdin = std::io::stdin();
        for line in stdin.lock().lines() {
            let result = line
                .map_err(|error| error.to_string())
                .and_then(|line| {
                    if line.len() > 65536 {
                        return Err("request exceeds 64 KiB".into());
                    }
                    serde_json::from_str::<Request>(&line).map_err(|error| error.to_string())
                });
            let stop = result.is_err();
            if sender.send(result).is_err() || stop {
                break;
            }
        }
    });

    let mut closing = false;
    let result = loop {
        if let Some(error) = live.take_error() {
            break Err(error);
        }
        if let Some(disconnect) = live.take_disconnect() {
            let _ = emit(serde_json::json!({"type": "disconnect", "reason": disconnect.to_string()}));
            break Err("server disconnected".into());
        }
        match receiver.recv_timeout(Duration::from_millis(10)) {
            Ok(Ok(request)) => {
                if request.op == "leave" {
                    reply(request.id, Ok(serde_json::json!({"leaving": true})))?;
                    closing = true;
                    break Ok(());
                }
                reply(request.id.clone(), execute(&live, &request))?;
            }
            Ok(Err(error)) => {
                emit(serde_json::json!({"type": "protocol_error", "error": error}))?;
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                closing = true;
                break Ok(());
            }
        }
    };
    if closing {
        live.finish();
    } else {
        live.finish_checked()?;
    }
    Ok(result?)
}
