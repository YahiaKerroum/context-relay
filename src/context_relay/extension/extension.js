// Context Relay editor side.
// - Opens the next chat when a long chat hands off (requests that context-relay drops in ~/.context-relay/outbox).
// - Runs delegated batches in the Antigravity IDE when the "ide" runner is chosen.
// - Shows chats, handoffs and delegation jobs in a sidebar panel; mirrors its settings into config.json.
// VS Code handles "claude" requests, the Antigravity IDE handles "agy" ones.
const vscode = require('vscode');
const fs = require('fs');
const os = require('os');
const path = require('path');
const cp = require('child_process');

const HOME = process.env.CONTEXT_RELAY_HOME || path.join(os.homedir(), '.context-relay');
const OUTBOX = path.join(HOME, 'outbox');
const CLAIMED = path.join(OUTBOX, 'claimed');
const STATE = path.join(HOME, 'state');
const JOBS = path.join(HOME, 'jobs');
const CONFIG = path.join(HOME, 'config.json');
const POLL_MS = 1500;
const OPEN_WINDOW_AFTER_MS = 6000; // nobody owns the project: open a window for it
const EXPIRE_MS = 10 * 60 * 1000;

const isAgy = vscode.env.appName.toLowerCase().includes('antigravity');
const target = isAgy ? 'agy' : 'claude';
const appLabel = isAgy ? 'Antigravity' : 'Claude';
const norm = (p) => String(p || '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let out, bar, panel;

function readJson(p, fallback = null) {
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return fallback; }
}
function writeJson(p, data) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  const tmp = `${p}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2));
  fs.renameSync(tmp, p);
}
function relayConfig() { return readJson(CONFIG, {}) || {}; }
function install() { return readJson(path.join(HOME, 'install.json'), {}) || {}; }

function ownsPath(cwd) {
  const c = norm(cwd);
  return (vscode.workspace.workspaceFolders || []).some((f) => {
    const w = norm(f.uri.fsPath);
    return c === w || c.startsWith(w + '/') || w.startsWith(c + '/');
  });
}

// ------------------------------------------------------------ settings -> config.json

const MAP = [
  ['thresholdPct', 'threshold_pct'], ['warnAtPct', 'warn_at_pct'], ['autoOpenNewChat', 'auto_open_new_chat'],
  ['autoSubmit', 'auto_submit'], ['autoSubmitDelayMs', 'auto_submit_delay_ms'],
  ['continueIn', 'continue_in', (v) => (v === 'gemini' ? 'agy' : v)],
  ['openAppHandoffsInIde', 'open_app_handoffs_in_ide'], ['claudeWindowTokens', 'claude_window_tokens'],
  ['geminiWindowTokens', 'agy_window_tokens'], ['remindEvery', 'remind_every'],
  ['delegation.runner', 'delegation.runner'], ['delegation.model', 'delegation.model'],
  ['delegation.batchSize', 'delegation.batch_size'], ['delegation.concurrency', 'delegation.concurrency'],
  ['delegation.maxRetries', 'delegation.max_retries'],
  ['delegation.calibrationPerBatch', 'delegation.calibration_per_batch'],
  ['delegation.calibrationMinAccuracy', 'delegation.calibration_min_accuracy'],
  ['delegation.taskPermissions', 'delegation.task_permissions'],
  ['guard.dangerGate', 'guard.danger_gate'], ['guard.dataGuard', 'guard.data_guard'],
  ['guard.approvalTimeoutMin', 'guard.approval_timeout_min'], ['network.mode', 'network.mode'],
  ['network.dataSsids', 'network.data_ssids'], ['snapshots.enabled', 'snapshots.enabled'],
  ['telegram.notify', 'telegram.notify'], ['telegram.approvals', 'telegram.approvals'],
  ['awayAfterMin', 'away_after_min'], ['keepAwake', 'keep_awake'], ['lockedIsAway', 'locked_is_away'],
  ['autoSwitch.enabled', 'auto_switch.enabled'],
  ['stuck.enabled', 'stuck.enabled'], ['stuck.afterErrors', 'stuck.after_errors'], ['stuck.escalateAfter', 'stuck.escalate_after'],
  ['stuck.loopEdits', 'stuck.loop_edits'], ['stuck.escalate', 'stuck.escalate'],
  ['checks.enabled', 'checks.enabled'], ['checks.timeoutS', 'checks.timeout_s'], ['checks.maxRounds', 'checks.max_rounds'],
  ['inbox.autoRun', 'inbox.auto_run'], ['inbox.idleWaitS', 'inbox.idle_wait_s'],
  ['phone.permissionMode', 'phone.permission_mode'], ['phone.resumeMode', 'phone.resume_mode'],
  ['phone.timeoutMin', 'phone.timeout_min'], ['phone.defaultTool', 'phone.default_tool'],
  ['live.everyS', 'live.every_s'], ['live.maxMin', 'live.max_min'],
  ['ask.timeoutMin', 'ask.timeout_min'], ['ask.telegramAfterS', 'ask.telegram_after_s'],
  ['recovery.enabled', 'recovery.enabled'], ['recovery.graceS', 'recovery.grace_s'],
  ['kaggle.autoFetch', 'kaggle.auto_fetch'],
  ['night.maxHours', 'night.max_hours'], ['night.maxSessionsPerTask', 'night.max_sessions_per_task'],
  ['night.fallbackToGemini', 'night.fallback_to_gemini'],
  ['delegation.interRater.enabled', 'delegation.inter_rater.enabled'],
  ['delegation.interRater.fraction', 'delegation.inter_rater.fraction'],
  ['delegation.interRater.model', 'delegation.inter_rater.model'],
  ['guard.extraDangerPatterns', 'guard.extra_danger_patterns'],
  ['autopilot.maxSessions', 'autopilot.max_sessions'],
];

// Settings you set explicitly win; untouched ones leave config.json alone, so VS Code
// and the Antigravity IDE (which share config.json) don't fight over defaults.
function syncSettings(event) {
  const s = vscode.workspace.getConfiguration('contextRelay');
  const cfg = relayConfig();
  let changed = false;
  for (const [key, cfgKey, conv = (v) => v] of MAP) {
    const touched = event ? event.affectsConfiguration(`contextRelay.${key}`) : s.inspect(key).globalValue !== undefined;
    if (!touched) continue;
    const parts = cfgKey.split('.');
    let node = cfg;
    for (const p of parts.slice(0, -1)) node = node[p] = node[p] || {};
    const value = conv(s.get(key));
    if (node[parts.at(-1)] !== value) { node[parts.at(-1)] = value; changed = true; }
  }
  if (changed) { writeJson(CONFIG, cfg); out.appendLine('settings -> config.json'); }
}

// ------------------------------------------------------------ opening the next chat

function submitted(id) { return fs.existsSync(path.join(STATE, 'submitted', `${id}.json`)); }

function waitForFocus() {
  if (vscode.window.state.focused) return Promise.resolve();
  return new Promise((resolve) => {
    const d = vscode.window.onDidChangeWindowState((s) => { if (s.focused) { d.dispose(); resolve(); } });
  });
}

// The Claude chat box takes a prefilled prompt but no submit, so press Enter for it,
// only while this window is focused and in the foreground.
function pressEnter() {
  const proc = path.parse(process.execPath).name.replace(/'/g, "''");
  const ps = `
Add-Type @'
using System; using System.Runtime.InteropServices;
public class Fg { [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p); }
'@
$p = 0; [void][Fg]::GetWindowThreadProcessId([Fg]::GetForegroundWindow(), [ref]$p)
$n = (Get-Process -Id $p).ProcessName
if ($n -eq '${proc}') { (New-Object -ComObject WScript.Shell).SendKeys('~'); 'sent' } else { "skipped, foreground is $n" }`;
  return new Promise((resolve) => cp.execFile('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', ps],
    { windowsHide: true }, (err, stdout) => { out.appendLine(`enter: ${err ? err.message : stdout.trim()}`); resolve(); }));
}

async function ensureSent(req, focusInput) {
  for (let i = 0; i < 6; i++) { if (submitted(req.id)) return true; await sleep(500); }
  let nudged = false;
  for (let attempt = 1; attempt <= 4; attempt++) {
    if (!vscode.window.state.focused && !nudged) {
      nudged = true;
      vscode.window.showInformationMessage('Context Relay: the new chat is ready; it will be sent when you switch back to this window.');
    }
    await waitForFocus();
    await sleep(attempt === 1 ? 300 : 1000);
    if (submitted(req.id)) return true;
    await focusInput();
    await sleep(300);
    if (vscode.window.state.focused && process.platform === 'win32') await pressEnter();
    for (let i = 0; i < 10; i++) { await sleep(700); if (submitted(req.id)) return true; }
  }
  return false;
}

async function launchClaude(req) {
  await vscode.commands.executeCommand('claude-vscode.editor.open', undefined, req.prompt);
  if (!req.auto_submit) return true;
  await sleep(req.auto_submit_delay_ms || 2500);
  return ensureSent(req, () => vscode.commands.executeCommand('claude-vscode.focus').then(undefined, () => {}));
}

async function launchAgy(req) {
  await vscode.commands.executeCommand('antigravity.startNewConversation');
  await sleep(1500);
  await vscode.commands.executeCommand('antigravity.sendPromptToAgentPanel', req.prompt);
  if (req.auto_submit === false) return true;
  return ensureSent(req, () => vscode.commands.executeCommand('antigravity.panel.focus').then(undefined, () => {}));
}

async function launch(req) {
  const ok = await (target === 'claude' ? launchClaude(req) : launchAgy(req));
  if (req.kind === 'handoff') {
    out.appendLine(`handoff ${req.id}: chat opened, sent=${ok}`);
    const msg = ok
      ? `Context Relay: the ${req.from === 'agy' ? 'Gemini' : 'Claude'} chat hit its limit; a fresh ${appLabel} chat took over from ${path.basename(req.handoff_path)}.`
      : `Context Relay: a fresh ${appLabel} chat is open with the handoff. Press Enter in it to start.`;
    vscode.window.showInformationMessage(msg, 'Open handoff')
      .then((pick) => pick && vscode.window.showTextDocument(vscode.Uri.file(req.handoff_path)));
  } else {
    out.appendLine(`${req.kind} ${req.id}: sent to a new Antigravity conversation`);
  }
}

function claim(file, req) {
  fs.mkdirSync(CLAIMED, { recursive: true });
  try { fs.renameSync(file, path.join(CLAIMED, `${req.id}.${process.pid}.json`)); return true; } catch { return false; }
}

// No window has the project open: open one, and its copy of this extension takes the request.
function openProjectWindow(req) {
  try { fs.writeFileSync(path.join(OUTBOX, `${req.id}.opening`), String(process.pid), { flag: 'wx' }); } catch { return; }
  out.appendLine(`no ${req.target} window owns ${req.cwd}; opening one`);
  if (req.target === target) {
    vscode.commands.executeCommand('vscode.openFolder', vscode.Uri.file(req.cwd), { forceNewWindow: true });
    return;
  }
  const cli = req.target === 'claude' ? 'code'
    : (process.platform === 'win32' ? path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Antigravity IDE', 'bin', 'antigravity-ide.cmd') : 'antigravity-ide');
  cp.spawn(cli, ['--new-window', req.cwd], { shell: true, detached: true, windowsHide: true }).unref();
}

function canHandle(req) {
  if (req.target !== target) return false;
  if (target === 'claude') return !!vscode.extensions.getExtension('anthropic.claude-code');
  return true;
}

let busy = false;
async function pollOutbox() {
  if (!fs.existsSync(OUTBOX)) return;
  for (const name of fs.readdirSync(OUTBOX)) {
    if (!name.endsWith('.json')) continue;
    const file = path.join(OUTBOX, name);
    const req = readJson(file);
    if (!req) continue;
    const age = Date.now() - req.created * 1000;
    if (age > EXPIRE_MS && req.kind === 'handoff') { if (claim(file, req)) out.appendLine(`expired ${req.id}`); continue; }
    if (canHandle(req) && ownsPath(req.cwd)) {
      if (!claim(file, req)) continue;
      try { await launch(req); } catch (e) {
        out.appendLine(`launch failed: ${e && e.message}`);
        vscode.window.showWarningMessage(`Context Relay could not open the new chat (${e && e.message}).`);
      }
    } else if (age > OPEN_WINDOW_AFTER_MS && relayConfig().auto_open_new_chat !== false) {
      openProjectWindow(req);
    }
  }
}

// ------------------------------------------------------------ reading state

// every chat seen in the last 12 h (all projects); the panel shows this workspace's, the hover shows all
function allSessions() {
  const list = [];
  for (const tool of ['claude', 'agy']) {
    let names = [];
    try { names = fs.readdirSync(path.join(STATE, tool)); } catch { continue; }
    for (const f of names) {
      const st = readJson(path.join(STATE, tool, f));
      if (st && st.updated && st.pct !== undefined && st.title !== 'Gemini job' && Date.now() / 1000 - st.updated < 12 * 3600) list.push(st);
    }
  }
  return list.sort((a, b) => b.updated - a.updated);
}
function sessions() { return allSessions().filter((s) => ownsPath(s.cwd)).slice(0, 8); }

function chatName(s) { return s.title || `${s.tool === 'claude' ? 'Claude' : 'Gemini'} chat ${s.sid.slice(0, 6)}`; }

function hoverText(all, thr, running) {
  const md = new vscode.MarkdownString(undefined, true);
  md.appendMarkdown(`**Context Relay** · hands off at ${thr}%\n\n`);
  if (!all.length) md.appendMarkdown('_No chats in the last 12 hours._\n\n');
  else {
    md.appendMarkdown('| | Chat | Project | Context | |\n|:-:|:--|:--|:--|:--|\n');
    for (const s of all.slice(0, 12)) {
      const n = Math.round(Math.min(s.pct, 100) / 10);
      const meter = '▓'.repeat(n) + '░'.repeat(10 - n);
      const state = s.busy ? 'working' : s.status === 'ok' ? 'idle' : s.status;
      const warn = s.pct >= thr ? ' ⚠' : '';
      md.appendMarkdown(`| ${s.tool === 'claude' ? '🟣' : '🔵'} | ${chatName(s).replace(/\|/g, '/')} | ${path.basename(s.cwd)} | \`${meter}\` ${s.pct.toFixed(0)}%${warn} | ${state} |\n`);
    }
    md.appendMarkdown('\n🟣 Claude · 🔵 Gemini\n\n');
  }
  if (running) md.appendMarkdown(`$(loading~spin) ${running} Gemini job(s) running\n\n`);
  md.appendMarkdown('Click to open the Relay panel.');
  return md;
}

function jobs() {
  let ids = [];
  try { ids = fs.readdirSync(JOBS).sort().reverse().slice(0, 12); } catch { return []; }
  return ids.map((id) => {
    const job = readJson(path.join(JOBS, id, 'job.json'));
    const st = readJson(path.join(JOBS, id, 'status.json'), {}) || {};
    const rep = readJson(path.join(JOBS, id, 'report.json'));
    return job && { id, job, st, rep };
  }).filter(Boolean);
}

function handoffs() {
  const list = [];
  for (const f of vscode.workspace.workspaceFolders || []) {
    const dir = path.join(f.uri.fsPath, '.context-relay', 'handoffs');
    let names = [];
    try { names = fs.readdirSync(dir); } catch { continue; }
    for (const n of names) {
      const p = path.join(dir, n);
      try {
        const head = fs.readFileSync(p, 'utf8').split('\n', 1)[0].replace(/^#\s*(Handoff:\s*)?/i, '');
        list.push({ path: p, name: n, title: head, mtime: fs.statSync(p).mtimeMs });
      } catch { /* skip */ }
    }
  }
  return list.sort((a, b) => b.mtime - a.mtime).slice(0, 10);
}

function limits() {
  const l = readJson(path.join(HOME, 'claude_limits.json'));
  return l && l.hit && Date.now() / 1000 < l.until ? l : null;
}

// ------------------------------------------------------------ notifications

const warned = new Set();
const jobStates = new Map();
function notify(sess, jobList) {
  const cfg = relayConfig();
  const warnAt = cfg.warn_at_pct ?? 50;
  const thr = cfg.threshold_pct ?? 65;
  for (const s of sess) {
    if (warnAt > 0 && s.status === 'ok' && s.pct >= warnAt && s.pct < thr && !warned.has(s.sid)) {
      warned.add(s.sid);
      const who = s.tool === 'claude' ? 'Claude' : 'Gemini';
      vscode.window.showInformationMessage(`Context Relay: ${who} chat at ${s.pct.toFixed(0)}% context; it hands off at ${thr}%.`, 'Hand off now')
        .then((pick) => pick && forceHandoff(s.tool, s.sid));
    }
  }
  for (const { id, job, st, rep } of jobList) {
    const prev = jobStates.get(id);
    jobStates.set(id, st.state);
    if (prev === undefined || prev === st.state || !['done', 'failed', 'paused'].includes(st.state)) continue;
    const text = `Gemini job "${job.title}": ${st.state}${st.message ? ` (${st.message})` : ''}`;
    const actions = rep && rep.output ? ['Open output', 'Report'] : ['Report'];
    vscode.window[st.state === 'done' ? 'showInformationMessage' : 'showWarningMessage'](text, ...actions).then((pick) => {
      if (pick === 'Open output') openFile(rep.output);
      if (pick === 'Report') openFile(path.join(JOBS, id, 'report.json'));
    });
  }
}

// ------------------------------------------------------------ actions

function openFile(p) { if (p && fs.existsSync(p)) vscode.window.showTextDocument(vscode.Uri.file(p)); }

function forceHandoff(tool, sid) {
  const f = path.join(STATE, 'force', tool, sid);
  fs.mkdirSync(path.dirname(f), { recursive: true });
  fs.writeFileSync(f, String(Date.now()));
  vscode.window.showInformationMessage(
    `Context Relay: the ${tool === 'claude' ? 'Claude' : 'Gemini'} chat will write its handoff at its next step. ` +
    'If it is idle, send it any message (for example "hand off").');
}

async function pickSession() {
  const list = sessions().filter((s) => s.status === 'ok');
  if (!list.length) { vscode.window.showInformationMessage('Context Relay: no active chats in this workspace.'); return null; }
  const pick = await vscode.window.showQuickPick(list.map((s) => ({
    label: `${s.tool === 'claude' ? 'Claude' : 'Gemini'} ${s.pct.toFixed(0)}%`, description: s.sid.slice(0, 8), s })),
  { placeHolder: 'Which chat should hand off now?' });
  return pick && pick.s;
}

function relayCmd() {
  const inst = install();
  return { python: inst.python || (process.platform === 'win32' ? 'python' : 'python3'), args: inst.args || ['-m', 'context_relay'] };
}

function runRelay(args) {
  const c = relayCmd();
  return new Promise((resolve, reject) => cp.execFile(c.python, [...c.args, ...args],
    { windowsHide: true }, (err, stdout, stderr) => (err ? reject(new Error(stderr || err.message)) : resolve(stdout.trim()))));
}

// the Telegram bot token goes to the helper through stdin, never on a command line
function setTelegramToken() {
  return vscode.window.showInputBox({ title: 'Telegram bot token', password: true, ignoreFocusOut: true,
    prompt: 'Create a bot with @BotFather in Telegram (/newbot) and paste its token. It is stored only in ~/.context-relay.' })
    .then((token) => {
      if (!token) return;
      const c = relayCmd();
      const child = cp.spawn(c.python, [...c.args, 'telegram', 'setup', '--token-stdin'], { windowsHide: true });
      let outp = '';
      child.stdout.on('data', (d) => { outp += d; });
      child.stderr.on('data', (d) => { outp += d; });
      child.on('close', (code) => {
        if (code === 0) vscode.window.showInformationMessage(`Context Relay: ${outp.trim().split('\n').slice(-2).join(' ')}`);
        else vscode.window.showErrorMessage(`Context Relay: ${outp.trim().slice(0, 200)}`);
      });
      child.stdin.end(token + '\n');
    });
}

function workspaceDir() { const f = (vscode.workspace.workspaceFolders || [])[0]; return f ? f.uri.fsPath : os.homedir(); }

async function pickRunnerAndModel() {
  const s = vscode.workspace.getConfiguration('contextRelay');
  const runner = await vscode.window.showQuickPick([
    { label: 'cli', description: 'Antigravity CLI: headless, parallel, fastest' },
    { label: 'ide', description: 'Antigravity IDE: watch each batch in the agent panel' }],
  { placeHolder: `Run it how? (default ${s.get('delegation.runner')})` });
  if (!runner) return null;
  const models = ['gemini-3.8-flash-low', 'gemini-3.8-flash-medium', 'gemini-3.8-flash-high'];
  const def = s.get('delegation.model');
  const model = await vscode.window.showQuickPick([def, ...models.filter((m) => m !== def)], { placeHolder: 'Model' });
  return model && { runner: runner.label, model };
}

async function annotateDataset() {
  const cwd = workspaceDir();
  const file = await vscode.window.showOpenDialog({ defaultUri: vscode.Uri.file(cwd), canSelectMany: false,
    filters: { Data: ['jsonl', 'json', 'csv', 'tsv', 'txt'] }, openLabel: 'Annotate this file' });
  if (!file) return;
  let instructions = await vscode.window.showInputBox({ title: 'Annotation guidelines', ignoreFocusOut: true,
    prompt: 'Write the guidelines, or give the path of a .md / .txt file that holds them.' });
  if (!instructions) return;
  const asPath = path.isAbsolute(instructions) ? instructions : path.join(cwd, instructions);
  if (fs.existsSync(asPath) && fs.statSync(asPath).isFile()) instructions = fs.readFileSync(asPath, 'utf8');
  const fields = await vscode.window.showInputBox({ title: 'Output fields', ignoreFocusOut: true,
    value: 'label:positive|negative|neutral, confidence:number', prompt: 'name:type, ... where type is string, number, integer, boolean, a|b|c (one of), or type[]' });
  if (!fields) return;
  const calPick = await vscode.window.showQuickPick(['Choose a calibration file (recommended)', 'No calibration'],
    { placeHolder: 'Calibration: JSONL of items you labeled, each with "expected": {...}. Mixed blind into every batch.' });
  if (!calPick) return;
  let calibration = null;
  if (calPick.startsWith('Choose')) {
    const c = await vscode.window.showOpenDialog({ defaultUri: vscode.Uri.file(cwd), filters: { JSONL: ['jsonl'] } });
    if (!c) return;
    calibration = c[0].fsPath;
  }
  const rm = await pickRunnerAndModel();
  if (!rm) return;
  const scope = await vscode.window.showQuickPick(['Pilot: first 40 records', 'Everything'], { placeHolder: 'Start with a pilot?' });
  if (!scope) return;
  const args = ['annotate', '--cwd', cwd, '--input', file[0].fsPath, '--instructions', instructions, '--fields', fields,
    '--runner', rm.runner, '--model', rm.model];
  if (calibration) args.push('--calibration', calibration);
  if (scope.startsWith('Pilot')) args.push('--limit', '40');
  try {
    const jid = await runRelay(args);
    vscode.window.showInformationMessage(`Context Relay: Gemini job ${jid} started. Progress is in the Relay panel.`);
  } catch (e) { vscode.window.showErrorMessage(`Context Relay: ${e.message}`); }
}

async function delegateTask() {
  const task = await vscode.window.showInputBox({ title: 'Task for Gemini (Antigravity)', ignoreFocusOut: true,
    prompt: 'Be exact: scope (files), conventions, and how to tell it is done.' });
  if (!task) return;
  const rm = await pickRunnerAndModel();
  if (!rm) return;
  try {
    const jid = await runRelay(['delegate-task', '--cwd', workspaceDir(), '--task', task, '--runner', rm.runner, '--model', rm.model]);
    vscode.window.showInformationMessage(`Context Relay: Gemini job ${jid} started.`);
  } catch (e) { vscode.window.showErrorMessage(`Context Relay: ${e.message}`); }
}

async function onPanelMessage(m) {
  if (m.cmd === 'open') openFile(m.path);
  else if (m.cmd === 'handoff') forceHandoff(m.tool, m.sid);
  else if (m.cmd === 'cancel') runRelay(['job', 'cancel', m.id]).catch((e) => out.appendLine(e.message));
  else if (m.cmd === 'resume') runRelay(['job', 'resume', m.id]).catch((e) => out.appendLine(e.message));
  else if (m.cmd === 'annotate') annotateDataset();
  else if (m.cmd === 'task') delegateTask();
  else if (m.cmd === 'settings') vscode.commands.executeCommand('workbench.action.openSettings', 'contextRelay');
  else if (m.cmd === 'log') openFile(path.join(HOME, 'relay.log'));
  else if (m.cmd === 'undo') undoTurn().catch((e) => vscode.window.showErrorMessage(e.message));
  else if (m.cmd === 'mode') toggleMode();
  else if (m.cmd === 'pair') pairingHint(true);
  else if (m.cmd === 'dash') openDashboard();
  else if (m.cmd === 'drop') dropIdea();
  else if (m.cmd === 'rule') addRule();
  else if (m.cmd === 'watchlog') watchLog();
  else if (m.cmd === 'watchkaggle') watchKaggle();
  else if (m.cmd === 'nightadd') nightAdd();
  else if (m.cmd === 'nightstart') nightStart();
  else if (m.cmd === 'nightstop') relayDo(['night', 'stop']);
  else if (m.cmd === 'nightclear') relayDo(['night', 'clear']);
  else if (m.cmd === 'nightreport') vscode.workspace.openTextDocument({ content: plainText(nightState().report), language: 'markdown' }).then((d) => vscode.window.showTextDocument(d));
  else if (m.cmd === 'limitclear') writeJson(path.join(HOME, 'claude_limits.json'), { hit: false, ts: Date.now() / 1000 });
  else if (m.cmd === 'ctl') {
    if (m.what === 'resume') setFlag('pause', m.sid, false); else setFlag(m.what, m.sid, true);
    vscode.window.showInformationMessage(m.what === 'resume' ? 'Context Relay: resumed.' : m.what === 'pause' ? 'Context Relay: pausing at the next step.' : 'Context Relay: told to stop at the next step.');
  } else if (m.cmd === 'rm') {
    const map = { inbox: ['inbox', 'rm'], rule: ['rule', 'rm'], watch: ['watch', 'rm'], night: ['night', 'remove'] };
    relayDo([...map[m.kind], String(m.id)]);
  }
}

// ------------------------------------------------------------ safety and remote

const APPROVALS = path.join(HOME, 'approvals');
const shownApprovals = new Set();
function pollApprovals() {
  let names = [];
  try { names = fs.readdirSync(APPROVALS); } catch { return; }
  for (const n of names) {
    const p = path.join(APPROVALS, n);
    const a = readJson(p);
    if (!a || a.status !== 'pending' || shownApprovals.has(a.id)) continue;
    shownApprovals.add(a.id);
    const who = a.tool === 'claude' ? 'Claude' : 'Gemini';
    const what = a.kind === 'danger' ? `⚠️ ${who} wants to run a ${a.title}` : `📶 ${who} wants a ${a.title}`;
    vscode.window.showWarningMessage(`${what} in ${path.basename(a.cwd || '')}:\n${a.detail.slice(0, 400)}`,
      { modal: false }, 'Allow', 'Deny').then((pick) => {
      const cur = readJson(p);
      if (!pick || !cur || cur.status !== 'pending') return;
      writeJson(p, { ...cur, status: pick === 'Allow' ? 'approved' : 'denied', by: appLabel === 'Claude' ? 'VS Code' : 'Antigravity IDE' });
    });
  }
}

function daemonState() {
  const d = readJson(path.join(HOME, 'daemon.json'));
  return d && Date.now() / 1000 - d.ts < 20 ? d : null;
}

let lastDaemonStart = 0;
function ensureDaemon() {
  if (daemonState() || Date.now() - lastDaemonStart < 60000) return;
  const inst = install();
  if (!inst.python) return; // not installed through `context-relay setup`
  lastDaemonStart = Date.now();
  const pyw = inst.python.replace(/python\.exe$/i, 'pythonw.exe');
  cp.spawn(fs.existsSync(pyw) ? pyw : inst.python, [...(inst.args || ['-m', 'context_relay']), 'daemon'], { detached: true, stdio: 'ignore', windowsHide: true }).unref();
  out.appendLine('started the relay daemon');
}

let pairNagged = false;
function pairingHint(force) {
  const code = (readJson(path.join(HOME, 'pair_code.json')) || {}).code;
  const s = readJson(path.join(HOME, 'secrets.json'), {}) || {};
  if (s.telegram_chat_id) { if (force) vscode.window.showInformationMessage('Context Relay: Telegram is paired.'); return; }
  if (!s.telegram_token) { if (force) setTelegramToken(); return; }
  if (!code || (pairNagged && !force)) return;
  pairNagged = true;
  vscode.window.showInformationMessage(`Context Relay: to connect your phone, send ${code} to your Telegram bot.`,
    'Copy code').then((p) => p && vscode.env.clipboard.writeText(code));
}

function setMode(mode) {
  vscode.workspace.getConfiguration('contextRelay').update('network.mode', mode, vscode.ConfigurationTarget.Global);
}

async function toggleMode() {
  const net = readJson(path.join(HOME, 'network.json'), {}) || {};
  const pick = await vscode.window.showQuickPick([
    { label: 'auto', description: `follow the connection (now ${net.detected || '?'}${net.ssid ? ', ' + net.ssid : ''})` },
    { label: 'wifi', description: 'downloads unrestricted' },
    { label: 'data', description: 'big downloads ask first' }], { placeHolder: 'Network mode' });
  if (pick) setMode(pick.label);
}

async function undoTurn() {
  const cwd = workspaceDir();
  let snaps;
  try { snaps = JSON.parse(await runRelay(['snapshots', cwd])); } catch (e) { return vscode.window.showErrorMessage(e.message); }
  snaps = snaps.filter((s) => !s.label.startsWith('before undo'));
  if (!snaps.length) return vscode.window.showInformationMessage('Context Relay: no snapshots in this project yet (they start with the next agent turn).');
  const ago = (t) => { const m = Math.round((Date.now() / 1000 - t) / 60); return m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`; };
  const pick = await vscode.window.showQuickPick(snaps.map((s, i) => ({
    label: `${i === 0 ? '$(debug-step-back) ' : ''}${s.label.replace(/^turn: /, '') || '(turn)'}`,
    description: `${s.tool === 'claude' ? 'Claude' : s.tool === 'agy' ? 'Gemini' : s.tool} · ${ago(s.ts)}`, s })),
  { placeHolder: 'Go back to the state before which turn? (everything after it is undone)' });
  if (!pick) return;
  const changes = JSON.parse(await runRelay(['changes', cwd, pick.s.sha]));
  if (!changes.length) return vscode.window.showInformationMessage('Context Relay: nothing changed since then.');
  const list = changes.slice(0, 15).map(([st, f]) => `${{ M: 'modified', A: 'created', D: 'deleted' }[st] || st}: ${f}`).join('\n');
  const ok = await vscode.window.showWarningMessage(`Undo ${changes.length} file change(s)?`,
    { modal: true, detail: list + (changes.length > 15 ? `\n… and ${changes.length - 15} more` : '') + '\n\nThe current state is saved first, so this can be undone too.' }, 'Undo');
  if (ok !== 'Undo') return;
  const res = JSON.parse(await runRelay(['undo', cwd, pick.s.sha]));
  vscode.window.showInformationMessage(`Context Relay: ${res.changed.length} file(s) restored.`);
}

// ------------------------------------------------------------ inbox, rules, watchers, night shift, questions

const QUESTIONS = path.join(HOME, 'questions');
const DESKTOP = path.join(HOME, 'desktop');
const plainText = (s) => String(s || '').replace(/<[^>]+>/g, '').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

function inboxItems() {
  return ((readJson(path.join(HOME, 'inbox.json'), { items: [] }) || {}).items || [])
    .filter((i) => ['queued', 'running'].includes(i.status)).slice(-12);
}
function rulesList() {
  const r = readJson(path.join(HOME, 'rules.json'), {}) || {};
  return [...(r.global || []), ...Object.entries(r.projects || {}).filter(([k]) => ownsPath(k)).flatMap(([, v]) => v)];
}
function watcherList() {
  return ((readJson(path.join(HOME, 'watchers.json'), { items: [] }) || {}).items || []).filter((w) => w.active !== false);
}
function nightState() { return readJson(path.join(HOME, 'night.json'), { tasks: [], state: 'idle' }) || { tasks: [], state: 'idle' }; }
function pausedSids() {
  try { return fs.readdirSync(path.join(HOME, 'flags')).filter((n) => n.startsWith('pause-')).map((n) => n.slice(6)); } catch { return []; }
}

function setFlag(name, sid, on) {
  const dir = path.join(HOME, 'flags');
  fs.mkdirSync(dir, { recursive: true });
  const f = path.join(dir, `${name}-${sid.slice(0, 8)}`);
  if (on) fs.writeFileSync(f, String(Date.now())); else { try { fs.unlinkSync(f); } catch { /* already gone */ } }
}

async function relayDo(args, okMsg) {
  try {
    const res = await runRelay(args);
    if (okMsg) vscode.window.showInformationMessage(okMsg);
    return res;
  } catch (e) { vscode.window.showErrorMessage(`Context Relay: ${String(e.message).split('\n').slice(-2).join(' ')}`); return null; }
}

async function dropIdea() {
  const text = await vscode.window.showInputBox({ title: 'Drop an idea', ignoreFocusOut: true,
    prompt: 'It is done between the agent\'s tasks, or started on its own after a short wait if nothing is running.' });
  if (text) await relayDo(['inbox', 'add', text, '--cwd', workspaceDir()], 'Context Relay: idea queued.');
}

async function addRule() {
  const text = await vscode.window.showInputBox({ title: 'Pin a rule', ignoreFocusOut: true,
    prompt: 'e.g. "never touch the data/ folder" or "always write docstrings". Paths you name are protected too.' });
  if (!text) return;
  const scope = await vscode.window.showQuickPick(['This project', 'Every project'], { placeHolder: 'Where does it apply?' });
  if (!scope) return;
  const res = await relayDo(['rule', 'add', text, '--cwd', workspaceDir(), ...(scope === 'Every project' ? ['--global'] : [])]);
  if (res) {
    const r = JSON.parse(res);
    vscode.window.showInformationMessage(`Context Relay: rule #${r.id} pinned` + (r.protect.length ? `; also protecting ${r.protect.join(', ')} (edits wait for your OK)` : '') + '.');
  }
}

async function watchLog() {
  const f = await vscode.window.showOpenDialog({ defaultUri: vscode.Uri.file(workspaceDir()), canSelectMany: false, openLabel: 'Watch this log' });
  if (f) await relayDo(['watch', 'add', f[0].fsPath, '--cwd', workspaceDir()], 'Context Relay: watching it for NaN loss, crashes and stalls.');
}

async function watchKaggle() {
  const slug = await vscode.window.showInputBox({ title: 'Watch a Kaggle run', ignoreFocusOut: true, prompt: 'yourname/notebook-slug (runs you push through Claude are picked up automatically)' });
  if (slug) await relayDo(['watch', 'kaggle', slug, '--cwd', workspaceDir()], 'Context Relay: watching the Kaggle run.');
}

async function nightAdd() {
  const task = await vscode.window.showInputBox({ title: 'Night shift task', ignoreFocusOut: true,
    prompt: 'What should be done overnight? Be specific about scope and how to tell it is done.' });
  if (!task) return;
  const who = await vscode.window.showQuickPick(['Claude', 'Gemini (Antigravity)'], { placeHolder: 'Who does it?' });
  if (who) await relayDo(['night', 'add', task, '--tool', who.startsWith('Claude') ? 'claude' : 'agy', '--cwd', workspaceDir()], 'Context Relay: added to the night shift.');
}

async function nightStart() {
  const t = await vscode.window.showInputBox({ title: 'Start the night shift', ignoreFocusOut: true, value: '23:00', prompt: 'Start time (HH:MM), or leave empty to start now.' });
  if (t === undefined) return;
  await relayDo(['night', 'start', ...(t.trim() ? ['--at', t.trim()] : [])], t.trim() ? `Context Relay: night shift starts at ${t.trim()}. A report comes to Telegram and here.` : 'Context Relay: night shift starting.');
}

// agents asking you instead of guessing (ask_user)
const shownQuestions = new Set();
function pollQuestions() {
  let names = [];
  try { names = fs.readdirSync(QUESTIONS); } catch { return; }
  for (const n of names) {
    const p = path.join(QUESTIONS, n);
    const q = readJson(p);
    if (!q || q.status !== 'pending' || shownQuestions.has(q.id)) continue;
    shownQuestions.add(q.id);
    const who = q.tool === 'claude' ? 'Claude' : 'Gemini';
    const answer = (text) => { const cur = readJson(p); if (cur && cur.status === 'pending') writeJson(p, { ...cur, status: 'answered', answer: text, by: 'editor' }); };
    const decide = 'You decide: use your best judgement and state the assumption in your final message.';
    const opts = q.options || [];
    const head = `❓ ${who} asks (${path.basename(q.cwd || '')}): ${q.question}`;
    if (opts.length && opts.length <= 3) {
      vscode.window.showInformationMessage(head, ...opts, 'You decide').then((pick) => pick && answer(pick === 'You decide' ? decide : pick));
    } else if (opts.length) {
      vscode.window.showQuickPick([...opts, '💬 Other…', '🤷 You decide'], { title: `${who} asks`, placeHolder: q.question, ignoreFocusOut: true }).then(async (pick) => {
        if (!pick) return;
        if (pick.startsWith('💬')) { const t = await vscode.window.showInputBox({ title: q.question, ignoreFocusOut: true }); if (t) answer(t); }
        else answer(pick.startsWith('🤷') ? decide : pick);
      });
    } else {
      vscode.window.showInputBox({ title: `${who} asks`, prompt: q.question, ignoreFocusOut: true }).then((t) => t && answer(t));
    }
  }
}

// alerts from the daemon (watchers, stuck chats, night shift...) while you are at the desk
const WARN_KINDS = new Set(['stuck', 'limit', 'checks', 'offline']);
function pollDesktop() {
  let names = [];
  try { names = fs.readdirSync(DESKTOP).filter((n) => n.endsWith('.json')); } catch { return; }
  const away = (readJson(path.join(HOME, 'presence.json'), {}) || {}).away;
  for (const n of names) {
    const f = path.join(DESKTOP, n);
    const d = readJson(f);
    if (!d) continue;
    try { fs.mkdirSync(path.join(DESKTOP, 'claimed'), { recursive: true }); fs.renameSync(f, path.join(DESKTOP, 'claimed', `${n}.${process.pid}`)); } catch { continue; }
    if (away || Date.now() / 1000 - d.ts > 600) continue; // away: Telegram has it
    const text = plainText(d.text);
    (WARN_KINDS.has(d.kind) ? vscode.window.showWarningMessage : vscode.window.showInformationMessage)(text.slice(0, 300), 'Open dashboard')
      .then((pick) => pick && openDashboard());
  }
}

let lastCard = (readJson(path.join(HOME, 'away_card.json'), {}) || {}).ts || 0;
function pollCard() {
  const c = readJson(path.join(HOME, 'away_card.json'));
  if (!c || c.ts <= lastCard) return;
  lastCard = c.ts;
  if (Date.now() / 1000 - c.ts > 300) return;
  vscode.window.showInformationMessage(`${c.title}: ${c.plain.split('\n').slice(0, 3).join(' · ')}`, 'Show all').then(async (pick) => {
    if (pick) vscode.window.showTextDocument(await vscode.workspace.openTextDocument({ content: `${c.title}\n\n${c.plain}\n`, language: 'markdown' }));
  });
}

// ------------------------------------------------------------ dashboard

let dash;
async function openDashboard() {
  if (dash) { dash.reveal(); return; }
  dash = vscode.window.createWebviewPanel('contextRelayDashboard', 'Context Relay Dashboard', vscode.ViewColumn.Active, { enableScripts: true });
  dash.webview.html = dashboardHtml();
  const load = async () => {
    if (!dash || !dash.visible) return;
    try { dash.webview.postMessage(JSON.parse(await runRelay(['stats']))); } catch (e) { out.appendLine(`stats: ${e.message}`); }
  };
  dash.webview.onDidReceiveMessage((m) => { if (m.cmd === 'open') openFile(m.path); });
  const t = setInterval(load, 20000);
  dash.onDidChangeViewState(load);
  dash.onDidDispose(() => { clearInterval(t); dash = undefined; });
  load();
}

function dashboardHtml() {
  const nonce = Math.random().toString(36).slice(2);
  return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
  body { font-family: var(--vscode-font-family); color: var(--vscode-foreground); padding: 12px 24px 40px; max-width: 1100px; }
  h1 { font-size: 18px; font-weight: 600; margin: 8px 0 2px; }
  h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .07em; color: var(--vscode-descriptionForeground); margin: 28px 0 10px; }
  .sub { color: var(--vscode-descriptionForeground); font-size: 12px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px; }
  .card { border: 1px solid var(--vscode-widget-border, var(--vscode-panel-border)); border-radius: 6px; padding: 12px 14px; background: var(--vscode-editorWidget-background); }
  .card .n { font-size: 24px; font-weight: 600; }
  .card .l { font-size: 11px; color: var(--vscode-descriptionForeground); margin-top: 2px; }
  table { border-collapse: collapse; width: 100%; font-size: 12px; }
  th { text-align: left; font-weight: 500; color: var(--vscode-descriptionForeground); padding: 4px 8px; border-bottom: 1px solid var(--vscode-panel-border); }
  td { padding: 6px 8px; border-bottom: 1px solid var(--vscode-panel-border); vertical-align: middle; }
  .meter { display: inline-block; width: 70px; height: 6px; border-radius: 3px; background: var(--vscode-editorWidget-border, #555); vertical-align: middle; margin-right: 6px; overflow: hidden; }
  .meter i { display: block; height: 100%; background: var(--vscode-progressBar-background, #0e70c0); }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .claude { background: #a371f7; } .agy { background: #4c9aff; }
  .chain { border: 1px solid var(--vscode-panel-border); border-radius: 6px; padding: 10px 12px; margin-bottom: 12px; }
  .chain .t { font-weight: 600; font-size: 12px; margin-bottom: 8px; }
  .nodes { display: flex; flex-wrap: wrap; align-items: stretch; gap: 0; }
  .node { border: 1px solid var(--vscode-panel-border); border-radius: 6px; padding: 8px 10px; width: 210px; background: var(--vscode-editorWidget-background); font-size: 12px; }
  .node .top { color: var(--vscode-descriptionForeground); font-size: 11px; margin-top: 4px; }
  .node .add { color: var(--vscode-gitDecoration-addedResourceForeground, #3fb950); } .node .del { color: var(--vscode-gitDecoration-deletedResourceForeground, #f85149); }
  .link { align-self: center; padding: 0 8px; font-size: 11px; color: var(--vscode-descriptionForeground); text-align: center; width: 78px; }
  .files { font-size: 11px; color: var(--vscode-descriptionForeground); margin-top: 4px; word-break: break-all; }
  .kv { display: grid; grid-template-columns: 1fr auto; gap: 4px 20px; font-size: 12px; max-width: 420px; }
  svg text { fill: var(--vscode-descriptionForeground); font-size: 9px; }
  .empty { color: var(--vscode-descriptionForeground); font-size: 12px; }
  a { color: var(--vscode-textLink-foreground); cursor: pointer; }
</style></head><body>
<h1>Context Relay</h1><div class="sub" id="sub">Loading…</div>
<div id="root"></div>
<script nonce="${nonce}">
  const vscode = acquireVsCodeApi();
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const k = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n || 0);
  const ago = (t) => { const m = Math.round((Date.now() / 1000 - t) / 60); return m < 1 ? 'just now' : m < 60 ? m + ' min ago' : m < 1440 ? Math.round(m / 60) + ' h ago' : Math.round(m / 1440) + ' d ago'; };
  const dur = (s) => s >= 3600 ? (s / 3600).toFixed(1) + ' h' : Math.round(s / 60) + ' min';
  const card = (n, l) => '<div class="card"><div class="n">' + n + '</div><div class="l">' + l + '</div></div>';
  window.addEventListener('message', ({ data: s }) => {
    document.getElementById('sub').textContent = 'Updated ' + new Date().toLocaleTimeString() + ' · refreshes every 20 s';
    const c = s.counts, w = s.counts_week;
    let h = '<h2>Today</h2><div class="cards">' +
      card(s.totals.chats_today, 'chats active in the last 24 h') +
      card(k(s.totals.out_tokens), 'tokens written by the models today') +
      card(s.totals.handoffs_week, 'handoffs this week') +
      card(dur(s.saved_s), 'saved by Gemini on datasets (estimate, 14 days)') +
      card((c.turn_done || 0), 'agent turns finished') + '</div>';
    h += '<h2>Chats (7 days)</h2>';
    if (!s.chats.length) h += '<p class="empty">No chats recorded yet.</p>';
    else {
      h += '<table><tr><th>Chat</th><th>Project</th><th>Peak context</th><th>Model calls</th><th>Tokens written</th><th>State</th><th>Last active</th></tr>';
      for (const x of s.chats.slice(0, 25)) {
        const peak = x.peak_tokens || x.tokens || 0, pct = x.window ? 100 * peak / x.window : x.pct;
        h += '<tr><td><span class="dot ' + x.tool + '"></span>' + esc(x.title.slice(0, 56)) + '</td><td>' + esc(x.project) + '</td><td><span class="meter"><i style="width:' + Math.min(pct, 100) + '%"></i></span>' + k(peak) + ' · ' + pct.toFixed(0) + '%</td><td>' + (x.calls || '–') + '</td><td>' + k(x.out_tokens) + '</td><td>' + esc(x.status) + '</td><td>' + ago(x.updated) + '</td></tr>';
      }
      h += '</table>';
    }
    h += '<h2>Gemini work per day (records annotated)</h2>';
    const days = s.gemini_days, max = Math.max(1, ...days.map((d) => d.records));
    if (days.every((d) => !d.jobs)) h += '<p class="empty">No Gemini jobs in the last 14 days.</p>';
    else {
      const bw = 560 / days.length;
      h += '<svg viewBox="0 0 560 130" width="100%" style="max-width:700px">';
      days.forEach((d, i) => {
        const bh = 90 * d.records / max;
        h += '<rect x="' + (i * bw + 3) + '" y="' + (100 - bh) + '" width="' + (bw - 6) + '" height="' + Math.max(bh, d.jobs ? 2 : 0) + '" rx="2" fill="var(--vscode-progressBar-background, #0e70c0)"><title>' + d.day + ': ' + d.records + ' records, ' + d.batches + ' batches, ' + d.jobs + ' job(s), ~' + dur(d.saved_s) + ' saved</title></rect>';
        h += '<text x="' + (i * bw + bw / 2) + '" y="114" text-anchor="middle">' + d.day.slice(8) + '</text>';
        if (d.records) h += '<text x="' + (i * bw + bw / 2) + '" y="' + (96 - bh) + '" text-anchor="middle">' + d.records + '</text>';
      });
      h += '</svg>';
    }
    h += '<h2>Handoff chains</h2>';
    if (!s.chains.length) h += '<p class="empty">A chain appears when a chat hands off to a new one. Each box shows what that session changed.</p>';
    for (const ch of s.chains) {
      h += '<div class="chain"><div class="t">' + esc(ch.project) + '</div><div class="nodes">';
      ch.nodes.forEach((n, i) => {
        h += '<div class="node"><div><span class="dot ' + n.tool + '"></span><b>' + esc(n.title.slice(0, 40)) + '</b></div>' +
          '<div class="top">' + (n.pct != null ? n.pct.toFixed(0) + '% context · ' : '') + (n.first ? ago(n.first) : '') + '</div>' +
          '<div class="top">' + (n.files ? n.files + ' file' + (n.files > 1 ? 's' : '') + ' <span class="add">+' + n.added + '</span> <span class="del">−' + n.deleted + '</span>' : 'no file changes') + '</div>' +
          (n.top.length ? '<div class="files">' + n.top.map((f) => esc(f.split('/').pop())).join(', ') + '</div>' : '') + '</div>';
        if (i < ch.nodes.length - 1) h += '<div class="link">→<br>' + esc({ context: 'context full', limit: 'limit', undefined: '' }[n.why] || 'stuck') + (n.to_tool && n.to_tool !== n.tool ? '<br>' + (n.to_tool === 'agy' ? 'to Gemini' : 'to Claude') : '') + '</div>';
      });
      h += '</div></div>';
    }
    h += '<h2>What the safety nets did (24 h · 7 days)</h2><div class="kv">';
    const rows = [['Approvals handled', 'approval'], ['of which denied', 'approval_denied'], ['Turns undone', 'undo'], ['Checks run', 'checks'], ['Stuck moments', 'stuck'], ['Phone prompts', 'phone'], ['Questions asked', 'ask'], ['Ideas queued / delivered', 'inbox'], ['Connection drops', 'offline']];
    for (const [l, key] of rows) h += '<div>' + l + '</div><div>' + (c[key] || 0) + ' · ' + (w[key] || 0) + '</div>';
    h += '</div>';
    document.getElementById('root').innerHTML = h;
  });
</script></body></html>`;
}

// ------------------------------------------------------------ panel

class Panel {
  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = panelHtml();
    view.webview.onDidReceiveMessage(onPanelMessage);
    view.onDidDispose(() => { this.view = undefined; });
    refresh();
  }
  post(data) { if (this.view && this.view.visible) this.view.webview.postMessage(data); }
}

function panelHtml() {
  const nonce = Math.random().toString(36).slice(2);
  return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
  * { box-sizing: border-box; }
  body { font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); color: var(--vscode-foreground); padding: 0 10px 16px; overflow-x: hidden; }
  .row, h3 { flex-wrap: wrap; }
  .row > span, .card b { min-width: 0; overflow-wrap: anywhere; }
  h3 { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--vscode-descriptionForeground); margin: 16px 0 6px; display: flex; justify-content: space-between; align-items: center; }
  .card { border: 1px solid var(--vscode-widget-border, var(--vscode-panel-border)); border-radius: 4px; padding: 8px; margin-bottom: 8px; background: var(--vscode-sideBar-background); }
  .row { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
  .muted { color: var(--vscode-descriptionForeground); font-size: 11px; }
  .bar { position: relative; height: 6px; border-radius: 3px; background: var(--vscode-progressBar-background, #0e70c0); opacity: 1; margin: 6px 0 4px; background: var(--vscode-editorWidget-background); overflow: visible; }
  .fill { height: 100%; border-radius: 3px; background: var(--vscode-progressBar-background, #0e70c0); }
  .fill.warn { background: var(--vscode-editorWarning-foreground, #cca700); }
  .fill.over { background: var(--vscode-editorError-foreground, #f14c4c); }
  .tick { position: absolute; top: -3px; width: 2px; height: 12px; background: var(--vscode-foreground); opacity: .55; }
  .chip { font-size: 10px; padding: 1px 6px; border-radius: 8px; background: var(--vscode-badge-background); color: var(--vscode-badge-foreground); }
  button { font: inherit; font-size: 11px; color: var(--vscode-button-secondaryForeground); background: var(--vscode-button-secondaryBackground); border: none; border-radius: 3px; padding: 3px 8px; margin: 4px 4px 0 0; cursor: pointer; }
  button:hover { background: var(--vscode-button-secondaryHoverBackground); }
  button.primary { color: var(--vscode-button-foreground); background: var(--vscode-button-background); }
  button.primary:hover { background: var(--vscode-button-hoverBackground); }
  a { color: var(--vscode-textLink-foreground); cursor: pointer; text-decoration: none; }
  .empty { color: var(--vscode-descriptionForeground); font-size: 12px; margin: 4px 0 8px; }
  .icon { background: none; padding: 0 4px; margin: 0; color: var(--vscode-descriptionForeground); }
</style></head><body>
<div id="root"><p class="empty">Loading…</p></div>
<script nonce="${nonce}">
  const vscode = acquireVsCodeApi();
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const send = (cmd, extra = {}) => vscode.postMessage({ cmd, ...extra });
  const k = (n) => n >= 1e6 ? (n / 1e6).toFixed(n % 1e6 ? 2 : 0) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n);
  const ago = (t) => { const m = Math.round((Date.now() / 1000 - t) / 60); return m < 1 ? 'just now' : m < 60 ? m + ' min ago' : Math.round(m / 60) + ' h ago'; };
  function bar(pct, thr, warn) {
    const cls = thr && pct >= thr ? 'over' : thr && pct >= warn && warn > 0 ? 'warn' : '';
    return '<div class="bar"><div class="fill ' + cls + '" style="width:' + Math.min(pct, 100) + '%"></div>' +
      (thr ? '<div class="tick" style="left:' + thr + '%"></div>' : '') + '</div>';
  }
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-cmd]');
    if (el) send(el.dataset.cmd, JSON.parse(el.dataset.args || '{}'));
  });
  const btn = (label, cmd, args, primary) => '<button class="' + (primary ? 'primary' : '') + '" data-cmd="' + cmd + '" data-args=\\'' + esc(JSON.stringify(args || {})) + '\\'>' + label + '</button>';
  window.addEventListener('message', ({ data: d }) => {
    const dm = d.daemon;
    let h = '<div class="card"><div class="row"><span>' + (d.net.mode === 'data' ? '📶 <b>Data mode</b>' : '🛜 <b>Wi-Fi mode</b>') +
      (d.net.ssid ? ' <span class="muted">' + esc(d.net.ssid) + '</span>' : '') + '</span>' + btn('Switch', 'mode') + '</div>' +
      '<div class="row"><span>' + (!dm ? '⚪ helper not running' : dm.paired ? (dm.telegram ? '🟢 Telegram connected' : '🟠 Telegram offline') : '🟠 Telegram not paired') +
      (dm && dm.away ? ' · 🚶 away' : '') + (dm && dm.awake ? ' · ☕ keeping PC awake' : '') + '</span>' +
      (dm && !dm.paired ? btn('Pair', 'pair') : '') + '</div>' + btn('↩ Undo an agent turn…', 'undo') + btn('📊 Dashboard', 'dash') + '</div>';
    h += '<h3>Chats <span><button class="icon" data-cmd="settings" title="Settings">⚙</button><button class="icon" data-cmd="log" title="Log">≡</button></span></h3>';
    if (!d.sessions.length) h += '<p class="empty">No chats in this workspace in the last day. They appear after the first reply.</p>';
    for (const s of d.sessions) {
      const who = s.tool === 'claude' ? 'Claude' : 'Gemini';
      h += '<div class="card"><div class="row"><b>' + esc(s.title || who + ' chat') + '</b><span class="chip">' + esc(s.busy ? 'working' : s.status) + '</span></div>' +
        '<div class="muted">' + (s.tool === 'claude' ? '🟣 Claude' : '🔵 Gemini') + (s.model ? ' · ' + esc(s.model) : '') + '</div>' +
        bar(s.pct, d.thr, d.warn) +
        '<div class="row"><span>' + s.pct.toFixed(0) + '% · ' + k(s.tokens) + ' / ' + k(s.window) + '</span><span class="muted">' + esc(s.sid.slice(0, 8)) + ' · ' + ago(s.updated) + '</span></div>' +
        (s.status === 'ok' ? btn('Hand off now', 'handoff', { tool: s.tool, sid: s.sid }) : '') +
        (s.busy ? btn('⏸ Pause', 'ctl', { what: 'pause', sid: s.sid }) + btn('⏹ Stop', 'ctl', { what: 'stop', sid: s.sid }) : '') +
        (d.paused.includes(s.sid.slice(0, 8)) ? btn('▶ Resume', 'ctl', { what: 'resume', sid: s.sid }) : '') +
        (s.handoff_path ? btn('Open handoff', 'open', { path: s.handoff_path }) : '') + '</div>';
    }
    if (d.limits) {
      h += '<div class="card"><div class="row"><span>⛔ <b>Claude\\'s usage limit is reached</b></span>' + btn('Clear', 'limitclear') + '</div>' +
        '<div class="muted">Back around ' + new Date(d.limits.until * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + '. Handoffs continue in Gemini until then.</div></div>';
    }
    const rm = (kind, id) => '<button class="icon" data-cmd="rm" data-args=\\'' + esc(JSON.stringify({ kind, id })) + '\\'>✖</button>';
    h += '<h3>Inbox <span>' + btn('Drop an idea…', 'drop', {}, true) + '</span></h3>';
    if (!d.inbox.length) h += '<p class="empty">Ideas you drop here (or with /drop on Telegram) get done between the agent\\'s tasks.</p>';
    for (const i of d.inbox) h += '<div class="row"><span>' + (i.status === 'running' ? '🏃 ' : '⏳ ') + esc(i.text.slice(0, 80)) + '</span>' + rm('inbox', i.id) + '</div>';
    h += '<h3>Pinned rules <span>' + btn('Pin a rule…', 'rule', {}, true) + '</span></h3>';
    if (!d.rules.length) h += '<p class="empty">Rules every agent follows, e.g. "never touch the data/ folder".</p>';
    for (const r of d.rules) h += '<div class="row"><span>📌 ' + esc(r.text.slice(0, 70)) + (r.protect && r.protect.length ? ' <span class="muted">🛡 ' + esc(r.protect.join(', ')) + '</span>' : '') + '</span>' + rm('rule', r.id) + '</div>';
    h += '<h3>Watching <span>' + btn('Log…', 'watchlog') + btn('Kaggle…', 'watchkaggle') + '</span></h3>';
    if (!d.watchers.length) h += '<p class="empty">Training logs and Kaggle runs: alerts on NaN loss, crashes, stalls, finished runs.</p>';
    for (const w of d.watchers) h += '<div class="row"><span>' + (w.kind === 'log' ? '📄 ' : '🏃 ') + esc(w.name) + (w.kind === 'kaggle' && w.status ? ' <span class="muted">' + esc(w.status.toLowerCase()) + '</span>' : '') + '</span>' + rm('watch', w.id) + '</div>';
    const nt = d.night, ticon = { done: '✅', failed: '❌', stopped: '⏹', skipped: '⏭', queued: '⏳', running: '🏃' };
    h += '<h3>Night shift <span>' + btn('Add task…', 'nightadd', {}, true) + '</span></h3>';
    if (!nt.tasks.length) h += '<p class="empty">Queue tasks before bed; the agents work through them within your limits and you get a morning report.</p>';
    for (const t of nt.tasks) h += '<div class="row"><span>' + (ticon[t.status] || '•') + ' ' + esc(t.prompt.slice(0, 64)) + ' <span class="muted">' + (t.kind === 'agy' ? 'Gemini' : t.kind) + '</span></span>' + (nt.state === 'running' ? '' : rm('night', t.id)) + '</div>';
    if (nt.tasks.length) {
      h += '<div class="muted">' + esc(nt.state) + (nt.state === 'scheduled' && nt.start_at ? ' · starts ' + new Date(nt.start_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '') + '</div>';
      h += (['idle', 'done'].includes(nt.state) ? btn('🌙 Start…', 'nightstart', {}, true) : btn('Stop', 'nightstop')) + btn('Clear', 'nightclear') + (nt.report ? btn('Report', 'nightreport') : '');
    }
    h += '<h3>Delegate to Gemini (Antigravity)</h3>' + btn('Annotate a dataset…', 'annotate', {}, true) + btn('Delegate a task…', 'task', {}, true);
    h += '<p class="muted">Or ask Claude: <code>/coordinate</code> plans the work, keeps the hard parts and sends the bulk to Gemini.</p>';
    for (const j of d.jobs) {
      const st = j.st, total = st.total || 0, doneN = st.done || 0, failN = st.failed || 0;
      const pct = total ? 100 * (doneN + failN) / total : 0;
      const cal = st.cal_n ? ' · calibration ' + Math.round(100 * st.cal_ok / st.cal_n) + '%' : '';
      h += '<div class="card"><div class="row"><b>' + esc(j.job.title) + '</b><span class="chip">' + esc(st.state) + '</span></div>' +
        '<div class="muted">' + esc(j.job.kind) + ' · ' + esc(j.job.runner) + ' · ' + esc(j.job.model) + ' · ' + esc(j.id) + '</div>' +
        (j.job.kind === 'dataset' ? bar(pct, 0, 0) + '<div class="row"><span>' + doneN + '/' + total + ' batches' + (failN ? ', ' + failN + ' failed' : '') + '</span><span class="muted">' + (st.retried || 0) + ' retried' + cal + '</span></div>' : '') +
        (st.message ? '<div class="muted">' + esc(st.message) + '</div>' : '') +
        (j.rep && j.rep.output ? btn('Output', 'open', { path: j.rep.output }) : '') +
        (j.rep ? btn('Report', 'open', { path: j.reportPath }) : '') + btn('Log', 'open', { path: j.logPath }) +
        (['running', 'queued'].includes(st.state) ? btn('Cancel', 'cancel', { id: j.id }) : '') +
        (['paused', 'done', 'failed', 'cancelled'].includes(st.state) && (failN || st.state !== 'done') ? btn('Resume', 'resume', { id: j.id }) : '') + '</div>';
    }
    h += '<h3>Handoffs</h3>';
    if (!d.handoffs.length) h += '<p class="empty">None yet in this workspace.</p>';
    for (const f of d.handoffs) h += '<div class="row"><a data-cmd="open" data-args=\\'' + esc(JSON.stringify({ path: f.path })) + '\\'>' + esc(f.title || f.name) + '</a><span class="muted">' + ago(f.mtime / 1000) + '</span></div>';
    document.getElementById('root').innerHTML = h;
  });
</script></body></html>`;
}

// ------------------------------------------------------------ loop

function refresh() {
  const cfg = relayConfig();
  const thr = cfg.threshold_pct ?? 65;
  const sess = sessions();
  const jobList = jobs();
  notify(sess, jobList);
  const all = allSessions();
  // the status bar shows the chat closest to its handoff, among this workspace's active ones
  const live = sess.filter((s) => s.status === 'ok' || s.status === 'requested');
  const top = live.reduce((a, s) => (!a || s.pct > a.pct ? s : a), null);
  const running = jobList.filter((j) => j.st.state === 'running').length;
  bar.text = (top ? `$(sync) ctx ${top.pct.toFixed(0)}%` : `$(sync) relay ${thr}%`) +
    (live.length > 1 ? ` · ${live.length} chats` : '') + (running ? `  $(loading~spin) ${running}` : '') +
    (limits() ? '  $(warning) limit' : '') + (nightState().state === 'running' ? '  $(moon) night' : '');
  bar.tooltip = hoverText(all, thr, running);
  bar.backgroundColor = top && top.pct >= thr ? new vscode.ThemeColor('statusBarItem.warningBackground') : undefined;
  pollApprovals();
  pollQuestions();
  pollDesktop();
  pollCard();
  try { writeJson(path.join(HOME, 'editor_heartbeat.json'), { ts: Date.now() / 1000, app: vscode.env.appName, pid: process.pid }); } catch { /* best effort */ }
  panel.post({
    daemon: daemonState(), net: readJson(path.join(HOME, 'network.json'), {}) || {},
    thr, warn: cfg.warn_at_pct ?? 50, sessions: sess, limits: limits(), handoffs: handoffs(),
    inbox: inboxItems(), rules: rulesList(), watchers: watcherList(), night: nightState(), paused: pausedSids(),
    jobs: jobList.map((j) => ({ ...j, reportPath: path.join(JOBS, j.id, 'report.json'), logPath: path.join(JOBS, j.id, 'log.txt') })),
  });
}

async function tick() {
  if (busy) return;
  busy = true;
  try { await pollOutbox(); refresh(); ensureDaemon(); } catch (e) { out.appendLine(`tick: ${e && e.stack}`); } finally { busy = false; }
}

function activate(context) {
  out = vscode.window.createOutputChannel('Context Relay');
  bar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  bar.command = 'workbench.view.extension.contextRelay';
  bar.show();
  panel = new Panel();
  out.appendLine(`active in ${vscode.env.appName}, handling "${target}" requests from ${OUTBOX}`);
  try { syncSettings(); } catch (e) { out.appendLine(`settings sync: ${e.message}`); }
  setTimeout(() => pairingHint(false), 8000);
  const timer = setInterval(tick, POLL_MS);
  const cmd = (id, fn) => vscode.commands.registerCommand(id, fn);
  context.subscriptions.push(out, bar, { dispose: () => clearInterval(timer) },
    vscode.window.registerWebviewViewProvider('contextRelay.panel', panel),
    vscode.workspace.onDidChangeConfiguration((e) => { if (e.affectsConfiguration('contextRelay')) syncSettings(e); }),
    cmd('contextRelay.handoffNow', async () => { const s = await pickSession(); if (s) forceHandoff(s.tool, s.sid); }),
    cmd('contextRelay.undo', () => undoTurn().catch((e) => vscode.window.showErrorMessage(e.message))),
    cmd('contextRelay.toggleMode', toggleMode),
    cmd('contextRelay.openDashboard', openDashboard),
    cmd('contextRelay.dropIdea', dropIdea),
    cmd('contextRelay.addRule', addRule),
    cmd('contextRelay.watchLog', watchLog),
    cmd('contextRelay.watchKaggle', watchKaggle),
    cmd('contextRelay.nightAdd', nightAdd),
    cmd('contextRelay.nightStart', nightStart),
    cmd('contextRelay.pairTelegram', () => pairingHint(true)),
    cmd('contextRelay.setTelegramToken', setTelegramToken),
    cmd('contextRelay.annotateDataset', annotateDataset),
    cmd('contextRelay.delegateTask', delegateTask),
    cmd('contextRelay.openLatestHandoff', () => {
      const h = handoffs()[0];
      if (h) openFile(h.path); else vscode.window.showInformationMessage('Context Relay: no handoffs in this workspace yet.');
    }),
    cmd('contextRelay.openSettings', () => vscode.commands.executeCommand('workbench.action.openSettings', 'contextRelay')),
    cmd('contextRelay.showLog', () => openFile(path.join(HOME, 'relay.log'))));
  tick();
}

module.exports = { activate, deactivate() {} };
