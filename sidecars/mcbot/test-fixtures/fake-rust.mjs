import readline from 'node:readline'

const snapshot = {
  protocol: 776,
  level_loading: false,
  position: [1, 64, -2],
  yaw: 0,
  pitch: 0,
  health: 20,
  food: 20,
  experience_level: 3,
  selected: 0,
  held: { slot: 0, name: 'minecraft:bread', count: 2 },
  inventory: [{ slot: 0, name: 'minecraft:bread', count: 2 }],
  players: ['BingTang'],
  chat_sequence: 0,
}

console.log(JSON.stringify({ type: 'ready', ok: true, protocol: 776 }))
const lines = readline.createInterface({ input: process.stdin })
lines.on('line', (line) => {
  const request = JSON.parse(line)
  if (request.op === 'leave') {
    console.log(JSON.stringify({ id: request.id, ok: true, result: { leaving: true } }))
    process.exit(0)
  }
  let result = {}
  if (request.op === 'status') result = snapshot
  else if (request.op === 'events') result = { events: [], next: snapshot.chat_sequence }
  else if (request.op === 'chat') {
    snapshot.chat_sequence += 1
    result = { sent: true }
  } else if (['command', 'move', 'stop', 'use', 'select', 'respawn'].includes(request.op)) {
    result = { accepted: true }
  } else {
    console.log(JSON.stringify({ id: request.id, ok: false, error: 'unsupported' }))
    return
  }
  console.log(JSON.stringify({ id: request.id, ok: true, result }))
})
