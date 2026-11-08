/**
 * 浏览器侧：设置 → 「长期记忆」。
 *
 * 区域：
 *   概况  统计 + 库路径 + 最近事件
 *   模型  JEV / 提取 / Embedding 三槽设置（含保存即测试、向量索引重建）
 *   数据  事件 / 经验 / 认知 / 消息 / 引用 / 工作记忆 的通用表格（增删查改）
 *         列与可编辑字段来自内核 `op:'tables'`，新增表不需要改前端
 *   日志  shell.log / kernel.stderr.log 尾部，可自动刷新
 *
 * Host 接口（仅回环）：
 *   GET  /longmemory/stats
 *   POST /longmemory/api   {op: tables|list|get|insert|update|delete|settings.*|vec.*, ...}
 *   POST /longmemory/test  {kind: jev|extract|embedding}
 *   GET  /longmemory/logs?source=shell|kernel&lines=N
 */
window.__ModuleLoader__.load({
  id: '@local/dsh-longmemory',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    /**
     * 作用域样式表：内联样式做不了 :hover / :disabled / 粘性表头 / 深色模式，
     * 这些只能靠一次注入的 <style>。变量都挂在 .lm-root 上，不污染宿主。
     */
    const STYLE_ID = 'dsh-longmemory-styles'
    const STYLE_TEXT = `
.lm-root {
  --lm-surface: var(--dsw-alias-bg-primary, #fff);
  --lm-surface-2: var(--dsw-alias-bg-secondary, #f6f7f9);
  --lm-text: var(--dsw-alias-label-primary, #1a1a1a);
  --lm-text-dim: var(--dsw-alias-label-tertiary, #6b7280);
  --lm-border: var(--dsw-alias-border-l2, #d8dbe2);
  --lm-border-soft: var(--dsw-alias-border-l1, #eceef2);
  --lm-danger: var(--dsw-alias-state-error-primary, #c0392b);
  --lm-accent: var(--dsw-alias-label-primary, #1a1a1a);
  color: var(--lm-text);
}
@media (prefers-color-scheme: dark) {
  .lm-root {
    --lm-surface: #1b1e24;
    --lm-surface-2: #23272e;
    --lm-text: #e7e8ea;
    --lm-text-dim: #9aa0a8;
    --lm-border: #3a4048;
    --lm-border-soft: #2b3037;
    --lm-accent: #e7e8ea;
  }
}
.lm-root * { box-sizing: border-box; }
.lm-btn {
  font: inherit; font-size: 13px; cursor: pointer; padding: 4px 10px;
  border-radius: 6px; border: .5px solid var(--lm-border);
  background: transparent; color: var(--lm-text);
  transition: background-color .12s ease, border-color .12s ease, opacity .12s ease;
}
.lm-btn:hover:not(:disabled) { background: var(--lm-surface-2); }
.lm-btn:active:not(:disabled) { transform: translateY(.5px); }
.lm-btn:disabled { opacity: .45; cursor: not-allowed; }
.lm-btn:focus-visible { outline: 2px solid var(--lm-accent); outline-offset: 1px; }
.lm-btn--primary { background: var(--lm-accent); color: var(--lm-surface); border-color: var(--lm-accent); }
.lm-btn--primary:hover:not(:disabled) { opacity: .88; }
.lm-btn--danger { color: var(--lm-danger); border-color: var(--lm-danger); }
.lm-btn--tab { border: 0; border-bottom: 2px solid transparent; border-radius: 0;
  padding: 6px 2px 8px; color: var(--lm-text-dim); white-space: nowrap; }
.lm-btn--tab:hover:not(:disabled) { background: transparent; color: var(--lm-text); }
.lm-btn--tab[data-active="true"] { color: var(--lm-text); border-bottom-color: var(--lm-accent); }
.lm-input, .lm-select, .lm-textarea {
  font: inherit; font-size: 13px; padding: 5px 8px; min-width: 0;
  border-radius: 6px; border: .5px solid var(--lm-border);
  background: var(--lm-surface); color: var(--lm-text);
}
.lm-input::placeholder { color: var(--lm-text-dim); }
.lm-input:focus, .lm-select:focus, .lm-textarea:focus {
  outline: none; border-color: var(--lm-accent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--lm-accent) 18%, transparent);
}
.lm-tabs { display: flex; gap: 14px; overflow-x: auto; scrollbar-width: thin;
  border-bottom: .5px solid var(--lm-border); }
.lm-tabs::-webkit-scrollbar { height: 6px; }
.lm-tabs::-webkit-scrollbar-thumb { background: var(--lm-border); border-radius: 3px; }
.lm-scroll { overflow: auto; border: .5px solid var(--lm-border);
  border-radius: 8px; background: var(--lm-surface); }
.lm-scroll::-webkit-scrollbar { width: 10px; height: 10px; }
.lm-scroll::-webkit-scrollbar-thumb {
  background: var(--lm-border); border-radius: 5px;
  border: 2px solid var(--lm-surface);
}
.lm-scroll::-webkit-scrollbar-track { background: transparent; }
.lm-table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 12px; }
.lm-table th {
  position: sticky; top: 0; z-index: 1; text-align: left; padding: 6px 8px;
  background: var(--lm-surface-2); color: var(--lm-text-dim); font-weight: 500;
  border-bottom: .5px solid var(--lm-border); white-space: nowrap;
}
.lm-table td { padding: 6px 8px; vertical-align: top;
  border-bottom: .5px solid var(--lm-border-soft); }
.lm-table tbody tr:hover { background: var(--lm-surface-2); }
.lm-table tbody tr:last-child td { border-bottom: 0; }
.lm-clip { display: inline-block; max-width: min(340px, 42vw);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  vertical-align: bottom; }
.lm-empty { padding: 24px 12px; text-align: center; color: var(--lm-text-dim);
  font-size: 12px; }
.lm-rowbtn { width: 100%; text-align: left; display: block; border: 0;
  border-radius: 6px; padding: 8px 10px; background: transparent;
  color: var(--lm-text); cursor: pointer; }
.lm-rowbtn:hover { background: var(--lm-surface-2); }
.lm-rowbtn:focus-visible { outline: 2px solid var(--lm-accent); outline-offset: -2px; }
.lm-rowbtn + .lm-rowbtn { border-top: .5px solid var(--lm-border-soft); }
.lm-pager { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: 12px; color: var(--lm-text-dim); }
.lm-pager input[type="number"] { width: 56px; }
.lm-modal { position: fixed; inset: 0; background: rgba(0, 0, 0, .42);
  display: flex; align-items: center; justify-content: center; z-index: 50;
  padding: 16px; }
.lm-dialog { background: var(--lm-surface); color: var(--lm-text);
  border: .5px solid var(--lm-border); border-radius: 10px;
  width: min(680px, 94vw); max-height: 86vh; display: flex;
  flex-direction: column; gap: 10px; padding: 16px; }
.lm-dialog__body { overflow: auto; display: flex; flex-direction: column;
  gap: 10px; min-height: 0; }
.lm-dialog__foot { display: flex; gap: 8px; justify-content: flex-end;
  border-top: .5px solid var(--lm-border-soft); padding-top: 10px; }
.lm-pre { font-family: var(--dsw-font-mono, monospace); font-size: 11px;
  line-height: 1.55; margin: 0; white-space: pre-wrap; word-break: break-word; }
.lm-card { border: .5px solid var(--lm-border); border-radius: 8px; padding: 10px 12px;
  background: var(--lm-surface); }
.lm-label { font-size: 12px; color: var(--lm-text-dim); }
.lm-error { color: var(--lm-danger); font-size: 13px; }
.lm-mono { font-family: var(--dsw-font-mono, monospace); font-size: 12px;
  word-break: break-all; color: var(--lm-text-dim); }
`

    /** 注入一次；非浏览器环境保护。 */
    function ensureStyles() {
      try {
        if (typeof document === 'undefined' || !document?.head) return
        if (document.getElementById(STYLE_ID)) return
        const tag = document.createElement('style')
        tag.id = STYLE_ID
        tag.textContent = STYLE_TEXT
        document.head.appendChild(tag)
      } catch {
        /* 非浏览器环境忽略 */
      }
    }
    ensureStyles()

    /** 统一按钮：semantic 变体 + 禁用态（内联样式做不到 :hover/:disabled）。 */
    function Btn({ variant, active, children, ...rest }) {
      const cls = ['lm-btn']
      if (variant === 'primary') cls.push('lm-btn--primary')
      if (variant === 'danger') cls.push('lm-btn--danger')
      if (variant === 'tab') cls.push('lm-btn--tab')
      return h('button', {
        type: 'button',
        ...rest,
        className: cls.join(' '),
        ...(variant === 'tab' ? { 'data-active': active ? 'true' : 'false' } : {}),
      }, children)
    }

    /** 空态：表/列表没有数据时别留一片空白。 */
    function Empty({ children }) {
      return h('div', { className: 'lm-empty' }, children ?? '暂无数据')
    }

    /**
     * 分页条：页码 + 每页条数 + 首/尾跳转。
     * 列表滚动区独立，分页条始终可见（不随内容滚走）。
     */
    function Pager({ offset, limit, total, onChange, onLimitChange, loading }) {
      const pages = Math.max(1, Math.ceil((Number(total) || 0) / Math.max(1, limit)))
      const page = Math.min(pages, Math.floor(offset / Math.max(1, limit)) + 1)
      const from = total === 0 ? 0 : offset + 1
      const to = Math.min(offset + limit, total)
      const jump = value => {
        const next = Math.max(1, Math.min(pages, Number(value) || 1))
        if (next !== page) onChange((next - 1) * limit)
      }
      return h('div', { className: 'lm-pager' },
        h('span', null, `第 ${from}–${to} 条 / 共 ${total} 条`),
        h('span', null,
          Btn({ disabled: offset <= 0 || loading, onClick: () => onChange(0) }, '« 首页'),
          ' ',
          Btn({
            disabled: offset <= 0 || loading,
            onClick: () => onChange(Math.max(0, offset - limit)),
          }, '‹ 上一页'),
          ' ',
          Btn({
            disabled: offset + limit >= total || loading,
            onClick: () => onChange(offset + limit),
          }, '下一页 ›'),
          ' ',
          Btn({
            disabled: offset + limit >= total || loading,
            onClick: () => onChange(Math.max(0, (pages - 1) * limit)),
          }, '末页 »')),
        h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: '4px' } },
          '第',
          h('input', {
            className: 'lm-input', type: 'number', min: 1, max: pages,
            defaultValue: page, key: `page-${page}`,
            onKeyDown: e => { if (e.key === 'Enter') jump(e.currentTarget.value) },
            onBlur: e => jump(e.currentTarget.value),
            'aria-label': '跳转到页码',
          }),
          '/', pages, '页'),
        h('label', { style: { display: 'inline-flex', alignItems: 'center', gap: '4px' } },
          '每页',
          h('select', {
            className: 'lm-select',
            value: String(limit),
            onChange: e => onLimitChange?.(Number(e.target.value)),
            'aria-label': '每页条数',
          }, ...[10, 20, 50, 100].map(n => h('option', { key: n, value: String(n) }, n)))))
    }

    const TABS = [
      { id: 'overview', label: '概况' },
      { id: 'models', label: '模型' },
      { id: 'event', label: '事件' },
      { id: 'experience', label: '经验' },
      { id: 'cognition', label: '认知' },
      { id: 'insight', label: '洞见' },
      { id: 'self_configured', label: '自我设定' },
      { id: 'self_notes', label: '模型自述' },
      { id: 'message', label: '消息' },
      { id: 'memory', label: '引用' },
      { id: 'digest', label: '工作记忆' },
      { id: 'logs', label: '日志' },
    ]

    const PREVIEW = {
      event: ['id', 'title', 'summary', 'session_id', 'origin_project', 'needs_review'],
      experience: ['id', 'content', 'kind', 'importance', 'origin_project', 'status'],
      cognition: ['id', 'subject', 'content', 'kind', 'origin_project', 'status'],
      insight: ['id', 'content', 'scope', 'importance', 'confidence', 'status'],
      message: ['_rowid', 'seq', 'role', 'session_id', 'message_id', 'content'],
      memory: ['_rowid', 'seq', 'session_id', 'message_id', 'event_id'],
      digest: ['session_id', 'revision', 'updated_at', 'content'],
    }

    const S = {
      wrap: { display: 'flex', flexDirection: 'column', gap: '12px',
              maxWidth: '1000px', minWidth: 0 },
      head: { display: 'flex', alignItems: 'baseline', gap: '12px', flexWrap: 'wrap' },
      title: { margin: 0, fontSize: '18px', fontWeight: 600 },
      bar: { display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap',
             minWidth: 0 },
      barTrack: { height: '4px', borderRadius: '2px', overflow: 'hidden',
                  background: 'var(--lm-border-soft)' },
      barFill: { height: '100%', background: 'var(--lm-accent)' },
      input: { flex: '1 1 180px', minWidth: 0 },
      clip: {},
      cards: { display: 'grid', gap: '10px',
               gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))' },
      cardLabel: { fontSize: '12px', color: 'var(--lm-text-dim)' },
      cardValue: { fontSize: '20px', fontWeight: 600, lineHeight: 1.2 },
      mono: { fontFamily: 'var(--dsw-font-mono, monospace)', fontSize: '12px',
              wordBreak: 'break-all', color: 'var(--lm-text-dim)' },
      field: { display: 'flex', flexDirection: 'column', gap: '3px', minWidth: 0 },
      label: { fontSize: '12px', color: 'var(--lm-text-dim)' },
      scroll: { maxHeight: 'min(58vh, 520px)' },
    }

    async function callApi(payload) {
      const response = await fetch('/longmemory/api', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload ?? {}),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(data?.error || `HTTP ${response.status}`)
      if (data?.ok === false) throw new Error(data.error || '操作失败')
      return data.result
    }

    function clip(value, max = 80) {
      const text = value === null || value === undefined ? '' : String(value)
      return text.length > max ? `${text.slice(0, max)}…` : text
    }

    /** 维护任务（重跑摘要 / 归纳洞见）：后台线程，轮询 maintenance.status 看进度。 */
    function Maintenance() {
      const [state, setState] = React.useState(null)
      const [busy, setBusy] = React.useState('')
      const [error, setError] = React.useState('')

      const load = React.useCallback(async () => {
        try {
          setState(await callApi({ op: 'maintenance.status' }))
          setError('')
        } catch (e) {
          setError(String(e?.message ?? e))
        }
      }, [])

      React.useEffect(() => { void load() }, [load])

      const running = Boolean(state?.resummarize?.running || state?.induce?.running)
      React.useEffect(() => {
        if (!running) return undefined
        const timer = setInterval(() => { void load() }, 1500)
        return () => clearInterval(timer)
      }, [running, load])

      const run = async op => {
        setBusy(op)
        setError('')
        try {
          await callApi({ op })
          await load()
        } catch (e) {
          setError(String(e?.message ?? e))
        } finally {
          setBusy('')
        }
      }

      const seconds = ms => `${(Number(ms || 0) / 1000).toFixed(1)}s`

      /** 一个任务的进度/结果文案（没跑过就不显示结果行）。 */
      const snapshotText = (snap, kind) => {
        if (!snap) return null
        if (snap.running) {
          return kind === 'resummarize'
            ? `重跑中 ${snap.done}/${snap.total} · 成功 ${snap.ok} / 失败 ${snap.failed}`
            : `归纳中 ${snap.done}/${snap.total} 簇 · 新增 ${snap.created} 条`
        }
        if (snap.error) return `失败：${snap.error}`
        if (!snap.finished_at) return null
        if (kind === 'resummarize') {
          return `上次重跑：成功 ${snap.ok} / 失败 ${snap.failed}`
            + ` · ${seconds(snap.elapsed_ms)}${snap.note ? ` · ${snap.note}` : ''}`
        }
        return `上次归纳：新增 ${snap.created} 条洞见（${snap.clusters} 簇）`
          + ` · ${seconds(snap.elapsed_ms)}${snap.note ? ` · ${snap.note}` : ''}`
      }

      const pending = Number(state?.pending_events ?? 0)
      const resummarizeRunning = Boolean(state?.resummarize?.running)
      const induceRunning = Boolean(state?.induce?.running)
      return h('div', { className: 'lm-card', style: { display: 'flex', flexDirection: 'column', gap: '6px' } },
        h('div', { style: { display: 'flex', alignItems: 'baseline', gap: '8px' } },
          h('strong', { style: { fontSize: '13px' } }, '维护'),
          state && h('span', { style: S.cardLabel },
            `待复核事件 ${pending} · 洞见 ${state.insights ?? 0} 条`)),
        h('div', { style: S.bar },
          Btn({
            disabled: Boolean(busy) || resummarizeRunning || pending === 0,
            onClick: () => { void run('event.resummarize') },
          }, resummarizeRunning ? '重跑中…' : '重跑待复核事件摘要'),
          Btn({
            disabled: Boolean(busy) || induceRunning,
            onClick: () => { void run('insight.induce') },
          }, induceRunning ? '归纳中…' : '归纳洞见')),
        [snapshotText(state?.resummarize, 'resummarize'),
         snapshotText(state?.induce, 'induce')]
          .filter(Boolean)
          .map((text, index) => h('div', { key: index, style: S.cardLabel }, text)),
        h('div', { style: S.cardLabel },
          '重跑：把待复核事件的摘要补上（成功才落经验/认知）；'
          + '归纳：把同一主题的多条经验提炼成通用规则（两路聚类：线索 + 向量相似度）。'),
        error && h('div', { className: 'lm-error' }, error))
    }

    function Overview() {
      const [state, setState] = React.useState({ status: 'loading', data: null, error: '' })
      const load = React.useCallback(async () => {
        try {
          const response = await fetch('/longmemory/stats', { cache: 'no-store' })
          if (!response.ok) throw new Error(`HTTP ${response.status}`)
          setState({ status: 'ready', data: await response.json(), error: '' })
        } catch (error) {
          setState({ status: 'error', data: null, error: String(error?.message ?? error) })
        }
      }, [])
      React.useEffect(() => { void load() }, [load])
      const s = state.data
      return h('div', { style: { display: 'flex', flexDirection: 'column', gap: '12px' } },
        h('div', { style: S.bar },
          Btn({ onClick: load }, '刷新'),
          state.status === 'loading' && h('span', { style: S.cardLabel }, '读取中…'),
          state.status === 'error'
            && h('span', { className: 'lm-error' }, `内核不可用：${state.error}`)),
        s && h('div', { style: S.cards },
          ...[['消息', s.messages], ['事件', s.events], ['经验', s.experiences],
              ['认知', s.cognitions]].map(([label, value]) =>
            h('div', { key: label, className: 'lm-card' },
              h('div', { style: S.cardLabel }, label),
              h('div', { style: S.cardValue }, String(value ?? 0))))),
        s && h('div', null,
          h('div', { style: S.cardLabel }, '内核版本'),
          h('div', { style: S.mono },
            `${s.plugin_version || '未知'}（协议 ${s.protocol_version || '?'}）`)),
        s && h('div', null,
          h('div', { style: S.cardLabel }, '记忆库'),
          h('div', { style: S.mono }, s.db_path || '')),
        s && h('div', null,
          h('div', { style: S.cardLabel }, '最近事件'),
          h('div', { style: S.mono },
            s.latest_event
              ? `${s.latest_event.title}${s.latest_event.needs_review ? '（待复核）' : ''}`
              : '（暂无）')),
        h(Maintenance))
    }

    /** 定位键：message/memory 用 rowid，其余按表自身主键（digest 是 session_id）。 */
    function locator(table, schema, row) {
      if (table === 'message' || table === 'memory') return row._rowid
      if (schema?.pk && schema.pk !== 'id') return row[schema.pk]
      return row.id
    }

    function EditDialog({ table, schema, mode, initial, onCancel, onSave }) {
      // 编辑只给业务列；主键与 seq 不在其中（改了会让正文与引用脱钩）
      const writable = (mode === 'edit' ? schema?.updatable : schema?.writable) ?? []
      const columns = schema?.columns ?? []
      const columnOf = name => columns.find(item => item.name === name) ?? {}
      const required = name => {
        const column = columnOf(name)
        return Boolean(column.notnull && !column.pk && column.default == null)
      }
      const [values, setValues] = React.useState(() => {
        const start = {}
        for (const name of writable) start[name] = initial[name] ?? ''
        return start
      })
      const missing = writable.filter(name =>
        required(name) && String(values[name] ?? '').trim() === '')
      const submit = () => {
        const payload = {}
        for (const [name, value] of Object.entries(values)) {
          const text = typeof value === 'string' ? value.trim() : value
          // 新增时空值不提交：让列默认值生效，否则会撞 NOT NULL
          if (mode === 'create' && (text === '' || text === null || text === undefined)) continue
          payload[name] = text
        }
        onSave(payload)
      }
      React.useEffect(() => {
        const onKey = e => { if (e.key === 'Escape') onCancel() }
        window.addEventListener('keydown', onKey)
        return () => window.removeEventListener('keydown', onKey)
      }, [onCancel])
      return h('div', {
        className: 'lm-modal', role: 'dialog', 'aria-modal': 'true',
        onClick: e => { if (e.target === e.currentTarget) onCancel() },
      },
        h('div', { className: 'lm-dialog' },
          h('h3', { style: { margin: 0, fontSize: '16px' } },
            `${mode === 'create' ? '新增' : '编辑'} ${table}`),
          h('div', { className: 'lm-dialog__body' },
            ...writable.map(name => h('label', { key: name, style: S.field },
              h('span', { style: S.label },
                name, required(name) ? ' *' : '',
                columnOf(name).type ? `（${columnOf(name).type}）` : ''),
              h('input', {
                className: 'lm-input',
                value: values[name] ?? '',
                placeholder: mode === 'create' ? '留空则用默认值' : '',
                onChange: e => setValues(prev => ({ ...prev, [name]: e.target.value })),
              }))),
            missing.length > 0 && h('div', { className: 'lm-error' },
              `必填：${missing.join('、')}`)),
          h('div', { className: 'lm-dialog__foot' },
            Btn({ onClick: onCancel }, '取消'),
            Btn({ variant: 'primary', disabled: missing.length > 0, onClick: submit }, '保存'))))
    }

    function DataTable({ table, schema }) {
      const [rows, setRows] = React.useState([])
      const [total, setTotal] = React.useState(0)
      const [offset, setOffset] = React.useState(0)
      const [query, setQuery] = React.useState('')
      const [error, setError] = React.useState('')
      const [editing, setEditing] = React.useState(null)
      const [loading, setLoading] = React.useState(false)
      const [limit, setLimit] = React.useState(20)

      const columns = schema?.columns ?? []
      const sample = rows[0] ?? {}
      const preview = (PREVIEW[table] ?? []).filter(name =>
        name === '_rowid' || rows.length === 0 || name in sample)
      const shown = preview.length ? preview : columns.slice(0, 5).map(c => c.name)

      const load = React.useCallback(async (nextOffset, nextQuery, nextLimit) => {
        const size = nextLimit ?? limit
        setError('')
        setLoading(true)
        try {
          const result = await callApi({
            op: 'list', table, limit: size, offset: nextOffset, query: nextQuery,
          })
          setRows(result.rows)
          setTotal(result.total)
          setOffset(nextOffset)
        } catch (e) {
          setError(String(e?.message ?? e))
        } finally {
          setLoading(false)
        }
      }, [table, limit])

      React.useEffect(() => { void load(0, '', limit) }, [table]) // eslint-disable-line

      const remove = async row => {
        const key = locator(table, schema, row)
        if (!window.confirm(`删除 ${table} #${key}？该操作不可撤销。`)) return
        try {
          await callApi({ op: 'delete', table, id: key })
          // 删掉当前页最后一条时回退一页，避免停在空白页
          const nextOffset = rows.length === 1 && offset > 0
            ? Math.max(0, offset - limit) : offset
          await load(nextOffset, query, limit)
        } catch (e) {
          setError(String(e?.message ?? e))
        }
      }

      // 列表里的长文本是截断过的，编辑前取整行，避免把摘要/正文改短
      const openEdit = async row => {
        const key = locator(table, schema, row)
        setError('')
        try {
          const full = await callApi({ op: 'get', table, id: key })
          setEditing({ mode: 'edit', values: { ...full, _rowid: row._rowid } })
        } catch (e) {
          setError(String(e?.message ?? e))
        }
      }

      const save = async values => {
        try {
          if (editing.mode === 'create') {
            await callApi({ op: 'insert', table, values })
          } else {
            const key = locator(table, schema, editing.values)
            await callApi({ op: 'update', table, id: key, values })
          }
          const mode = editing.mode
          setEditing(null)
          await load(mode === 'create' ? 0 : offset, query, limit)
        } catch (e) {
          setError(String(e?.message ?? e))
        }
      }

      return h('div', { style: { display: 'flex', flexDirection: 'column', gap: '8px', minWidth: 0 } },
        h('div', { style: S.bar },
          h('input', {
            className: 'lm-input', style: S.input, placeholder: '搜索…', value: query,
            onChange: e => setQuery(e.target.value),
            onKeyDown: e => { if (e.key === 'Enter') void load(0, query, limit) },
          }),
          Btn({ onClick: () => load(0, query, limit) }, '搜索'),
          Btn({ onClick: () => { setQuery(''); void load(0, '', limit) } }, '清空'),
          Btn({ variant: 'primary', onClick: () => setEditing({ mode: 'create', values: {} }) },
            '新增'),
          h('span', { style: S.cardLabel }, `共 ${total} 条`)),
        error && h('div', { className: 'lm-error' }, error),
        // 表格滚动区：表头粘性，内容只在这个框里滚，分页条永远可见
        h('div', { className: 'lm-scroll', style: S.scroll },
          h('table', { className: 'lm-table' },
            h('thead', null, h('tr', null,
              ...shown.map(name => h('th', { key: name }, name)),
              h('th', { key: '_ops' }, '操作'))),
            h('tbody', null,
              rows.length === 0
                ? h('tr', null, h('td', {
                    colSpan: shown.length + 1,
                  }, h(Empty, null, loading ? '读取中…' : (query ? '没有匹配的记录' : '还没有数据'))))
                : rows.map((row, index) => h('tr', { key: row._rowid ?? index },
                    ...shown.map(name => h('td', { key: name },
                      h('span', { className: 'lm-clip', title: String(row[name] ?? '') },
                        clip(row[name])))),
                    h('td', { key: '_ops', style: { whiteSpace: 'nowrap' } },
                      Btn({ onClick: () => { void openEdit(row) } }, '编辑'),
                      ' ',
                      Btn({ variant: 'danger', onClick: () => remove(row) }, '删除'))))))),
        h(Pager, {
          offset, limit, total, loading,
          onChange: next => load(next, query, limit),
          onLimitChange: next => { setLimit(next); load(0, query, next) },
        }),
        editing && h(EditDialog, {
          table, schema, mode: editing.mode, initial: editing.values,
          onCancel: () => setEditing(null), onSave: save,
        }))
    }

    function Logs() {
      const [source, setSource] = React.useState('shell')
      const [lines, setLines] = React.useState([])
      const [auto, setAuto] = React.useState(true)
      const [error, setError] = React.useState('')

      const load = React.useCallback(async () => {
        try {
          const response = await fetch(
            `/longmemory/logs?source=${source}&lines=300`, { cache: 'no-store' })
          if (!response.ok) throw new Error(`HTTP ${response.status}`)
          const data = await response.json()
          setLines(data.lines ?? [])
          setError('')
        } catch (e) {
          setError(String(e?.message ?? e))
        }
      }, [source])

      React.useEffect(() => { void load() }, [load])
      React.useEffect(() => {
        if (!auto) return undefined
        const timer = setInterval(() => { void load() }, 5000)
        return () => clearInterval(timer)
      }, [auto, load])

      return h('div', { style: { display: 'flex', flexDirection: 'column', gap: '8px', minWidth: 0 } },
        h('div', { style: S.bar },
          ...[['shell', 'shell.log'], ['kernel', 'kernel.stderr.log']].map(([id, label]) =>
            Btn({
              key: id, variant: 'tab', active: source === id,
              onClick: () => setSource(id),
            }, label)),
          Btn({ onClick: load }, '刷新'),
          h('label', { style: { display: 'flex', gap: '4px', alignItems: 'center',
                                fontSize: '12px', color: 'var(--lm-text-dim)' } },
            h('input', { type: 'checkbox', checked: auto,
                         onChange: e => setAuto(e.target.checked) }),
            '每 5 秒自动刷新'),
          h('span', { style: S.cardLabel }, `${lines.length} 行`)),
        error && h('div', { className: 'lm-error' }, error),
        h('div', { className: 'lm-scroll', style: { ...S.scroll, maxHeight: 'min(62vh, 560px)' } },
          h('pre', {
            className: 'lm-pre',
            style: { padding: '10px 12px' },
          }, lines.join('\n') || '（日志为空）')))
    }

    const BROWSER_KIND = 'longmemory-browser'
    const BROWSER_ID = 'longmemory-browser'
    const BROWSER_TABLES = [
      ['event', '事件'], ['experience', '经验'], ['cognition', '认知'], ['insight', '洞见'],
    ]

    /** 右侧栏「长期记忆」浏览器：搜索 + 分页 + 详情（只读）。 */
    function MemoryBrowser() {
      const [table, setTable] = React.useState('event')
      const [query, setQuery] = React.useState('')
      const [rows, setRows] = React.useState([])
      const [total, setTotal] = React.useState(0)
      const [offset, setOffset] = React.useState(0)
      const [detail, setDetail] = React.useState(null)
      const [error, setError] = React.useState('')
      const [loading, setLoading] = React.useState(false)
      const [limit, setLimit] = React.useState(20)

      const load = React.useCallback(async (nextOffset, nextQuery, nextLimit) => {
        const size = nextLimit ?? limit
        setLoading(true)
        try {
          const result = await callApi({
            op: 'list', table, limit: size, offset: nextOffset, query: nextQuery,
          })
          setRows(result.rows)
          setTotal(result.total)
          setOffset(nextOffset)
          setError('')
        } catch (e) {
          setError(String(e?.message ?? e))
        } finally {
          setLoading(false)
        }
      }, [table, limit])

      React.useEffect(() => { void load(0, '', limit) }, [table]) // eslint-disable-line

      React.useEffect(() => {
        if (!detail) return undefined
        const onKey = e => { if (e.key === 'Escape') setDetail(null) }
        window.addEventListener('keydown', onKey)
        return () => window.removeEventListener('keydown', onKey)
      }, [detail])

      const openDetail = async row => {
        try {
          setDetail(await callApi({ op: 'get', table, id: row.id }))
          setError('')
        } catch (e) {
          setError(String(e?.message ?? e))
        }
      }

      // 侧栏高度约束：列表区 flex:1 + overflow:auto，分页条固定在底部
      return h('div', { className: 'lm-root',
                        style: { display: 'flex', flexDirection: 'column', gap: '8px',
                                 padding: '8px', height: '100%', minHeight: 0 } },
        h('div', { style: { ...S.bar, flexWrap: 'nowrap', overflowX: 'auto' } },
          ...BROWSER_TABLES.map(([id, label]) =>
            Btn({
              key: id, variant: 'tab', active: table === id,
              onClick: () => setTable(id),
            }, label))),
        h('div', { style: S.bar },
          h('input', {
            className: 'lm-input', style: S.input, placeholder: '搜索…', value: query,
            onChange: e => setQuery(e.target.value),
            onKeyDown: e => { if (e.key === 'Enter') void load(0, query, limit) },
          }),
          Btn({ onClick: () => load(0, query, limit) }, '搜索'),
          h('span', { style: S.cardLabel }, `共 ${total} 条`)),
        error && h('div', { className: 'lm-error' }, error),
        h('div', {
          className: 'lm-scroll',
          style: { flex: 1, minHeight: '160px', maxHeight: '100%',
                   display: 'flex', flexDirection: 'column' },
        },
          rows.length === 0
            ? h(Empty, null, loading ? '读取中…' : (query ? '没有匹配的记录' : '还没有数据'))
            : rows.map(row => h('button', {
                key: row.id, type: 'button', className: 'lm-rowbtn',
                onClick: () => openDetail(row),
              },
                h('div', { style: S.cardLabel },
                  `#${row.id} ${row.title ?? row.subject ?? row.kind ?? ''}`),
                h('div', { style: { fontSize: '12px', marginTop: '2px',
                                    display: '-webkit-box', WebkitLineClamp: 2,
                                    WebkitBoxOrient: 'vertical', overflow: 'hidden' } },
                  clip(row.summary ?? row.content, 160))))),
        h(Pager, {
          offset, limit, total, loading,
          onChange: next => load(next, query, limit),
          onLimitChange: next => { setLimit(next); load(0, query, next) },
        }),
        detail && h('div', {
          className: 'lm-modal', role: 'dialog', 'aria-modal': 'true',
          onClick: e => { if (e.target === e.currentTarget) setDetail(null) },
        },
          h('div', { className: 'lm-dialog' },
            h('div', { style: { ...S.bar, justifyContent: 'space-between' } },
              h('strong', null, `${table} #${detail.id ?? ''}`),
              Btn({ onClick: () => setDetail(null) }, '关闭')),
            h('div', { className: 'lm-dialog__body' },
              h('pre', { className: 'lm-pre', style: { padding: '8px 10px',
                                                       background: 'var(--lm-surface-2)',
                                                       borderRadius: '6px' } },
                JSON.stringify(detail, null, 2))))))
    }

    function MemoryBrowserTitle() {
      return h('span', null, '长期记忆')
    }

    function MemoryHeaderButton({ open } = {}) {
      return Btn({ title: '打开长期记忆', onClick: () => open?.() }, '记忆')
    }

    const SELF_KINDS = [
      ['identity', '身份', '我是谁、我的定位'],
    ]

    /** 自我设定（configured → 系统提示词）/ 模型自述（agent → 动态上下文）。 */
    function SelfSettings({ mode = 'configured' }) {
      const [items, setItems] = React.useState([])
      const [values, setValues] = React.useState({})
      const [status, setStatus] = React.useState('')
      const [error, setError] = React.useState('')
      const [preview, setPreview] = React.useState(null)
      const configured = mode === 'configured'

      const load = React.useCallback(async () => {
        try {
          const result = await callApi({ op: 'self.list', source: mode })
          const list = result.items ?? []
          setItems(list)
          const active = {}
          for (const item of list) {
            if (item.status === 'active' && active[item.kind] === undefined) {
              active[item.kind] = item.content
            }
          }
          setValues(active)
          setError('')
        } catch (e) {
          setError(String(e?.message ?? e))
        }
      }, [mode])

      React.useEffect(() => { void load() }, [load])

      const save = async kind => {
        setStatus('')
        try {
          await callApi({ op: 'self.set', kind, content: values[kind] ?? '' })
          await load()
          setStatus('已保存，下一轮生效')
        } catch (e) {
          setError(String(e?.message ?? e))
        }
      }

      const disable = async id => {
        if (!window.confirm(`停用 #${id}？停用后不再注入。`)) return
        try {
          await callApi({ op: 'self.disable', id })
          await load()
        } catch (e) {
          setError(String(e?.message ?? e))
        }
      }

      const showPreview = async () => {
        try {
          setPreview(await callApi({ op: 'core.preview' }))
          setError('')
        } catch (e) {
          setError(String(e?.message ?? e))
        }
      }

      return h('div', { className: 'lm-root',
                        style: { display: 'flex', flexDirection: 'column', gap: '10px' } },
        h('div', { style: { ...S.cardLabel, maxWidth: '760px' } },
          configured
            ? '「设置的自我」保存后进入系统提示词 <self_configured>；同类只保留最新一条，旧条目留作历史、可停用。'
            : '「模型自述」由模型通过 memory_self_write 写入，进入动态上下文 <self_notes>；你可以在这里停用。'),
        configured && h('div', { style: { display: 'grid', gap: '10px' } },
          ...SELF_KINDS.map(([kind, label, hint]) => h('div', { key: kind, style: S.field },
            h('span', { style: S.label }, `${label}（${hint}）`),
            h('input', {
              className: 'lm-input', style: S.input,
              value: values[kind] ?? '',
              placeholder: hint,
              onChange: e => setValues(prev => ({ ...prev, [kind]: e.target.value })),
            }),
            Btn({ onClick: () => save(kind) }, `保存${label}`)))),
        h('div', { style: S.bar },
          Btn({ onClick: showPreview }, '预览注入文本'),
          Btn({ onClick: load }, '刷新'),
          status && h('span', { style: S.cardLabel }, status)),
        error && h('div', { className: 'lm-error' }, error),
        preview && h('div', { style: { display: 'flex', flexDirection: 'column', gap: '6px' } },
          h('div', { style: S.cardLabel },
            `系统提示词 ${preview.text?.length ?? 0} 字符 · 自述 ${preview.notes_text?.length ?? 0} 字符 · 版本 ${preview.version || '无'}`),
          h('div', { className: 'lm-scroll', style: { maxHeight: '240px' } },
            h('pre', { className: 'lm-pre', style: { padding: '10px 12px' } },
              preview.text || '（系统提示词段为空）')),
          h('div', { className: 'lm-scroll', style: { maxHeight: '180px' } },
            h('pre', { className: 'lm-pre', style: { padding: '10px 12px' } },
              preview.notes_text || '（自述为空）'))),
        h('div', { style: S.cardLabel }, '历史'),
        h('div', { className: 'lm-scroll', style: { ...S.scroll, maxHeight: '320px' } },
          h('table', { className: 'lm-table' },
            h('thead', null, h('tr', null,
              ...['id', 'kind', 'content', 'revision', 'status'].map(name =>
                h('th', { key: name }, name)),
              h('th', { key: '_ops' }, '操作'))),
            h('tbody', null,
              items.length === 0
                ? h('tr', null, h('td', { colSpan: 6 }, h(Empty)))
                : items.map(item => h('tr', { key: item.id },
                  ...['id', 'kind', 'content', 'revision', 'status'].map(name =>
                    h('td', { key: name },
                      h('span', { className: 'lm-clip', title: String(item[name] ?? '') },
                        clip(item[name])))),
                  h('td', { key: '_ops' },
                    item.status === 'active' && Btn({
                      variant: 'danger', onClick: () => disable(item.id),
                    }, '停用'))))))))
    }

    /** 三槽默认值：与 src/js/model-config.js 保持一致（内核没存过设置时用这套）。 */
    const MODEL_FALLBACK = {
      'model.jev': { mode: 'host', provider: '', model: '', baseUrl: '',
                     apiKey: '', timeoutMs: 120000 },
      'model.extract': { mode: 'host', provider: '', model: '', baseUrl: '',
                         apiKey: '', timeoutMs: 120000 },
      embedding: { flavor: 'openai', baseUrl: 'http://127.0.0.1:11434/v1',
                   model: 'bge-m3', apiKey: '', dim: 1024, batchSize: 16,
                   timeoutMs: 60000 },
    }

    const MODEL_CARDS = [
      ['model.jev', 'JEV 切分',
       '判断每条消息是「新事件 / 延续当前事件 / 重开旧事件」，只输出结构化结果，调用最频繁。'],
      ['model.extract', '摘要与提取',
       '事件摘要、经验、认知，以及合并、洞见、模型自述等其它文本调用。'],
    ]

    const MODEL_KIND = { 'model.jev': 'jev', 'model.extract': 'extract', embedding: 'embedding' }

    /** ANN 状态文案：首次检索前索引还没装载，别让「0 条」看起来像坏了。 */
    function annLabel(vec) {
      const ann = vec?.ann
      if (!ann?.available) return '精确 KNN（未装 usearch）'
      const loaded = Object.values(ann.indexed || {})
        .reduce((sum, value) => sum + Number(value || 0), 0)
      return `ANN ${ann.backend}（${loaded ? `${loaded} 条已装载` : '首次检索时装载'}）`
    }

    async function postJson(url, payload) {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload ?? {}),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok || data?.ok === false) {
        throw new Error(data?.message || data?.error || `HTTP ${response.status}`)
      }
      return data
    }

    /** 模型设置：JEV / 提取 / Embedding 三槽，key 存内核 app_setting（本地库）。 */
    function ModelSettings() {
      const [settings, setSettings] = React.useState(null)
      const [dirty, setDirty] = React.useState({})
      const [busy, setBusy] = React.useState('')
      const [status, setStatus] = React.useState('')
      const [error, setError] = React.useState('')
      const [vec, setVec] = React.useState(null)
      const [rebuild, setRebuild] = React.useState(null)
      // 点击后、后端确认「已开始」之前，轮询到的旧快照不能把「重建中」提前收掉
      const startAck = React.useRef(true)

      // 读不到状态时保留上一次的显示：一次失败不能把「重建中」收掉
      const loadVec = React.useCallback(async () => {
        try {
          const status = await callApi({ op: 'vec.status' })
          setVec(status)
          return status
        } catch {
          return null
        }
      }, [])

      /** 内核的 reindex 快照 → 界面状态（running 结束后给 done / error）。 */
      const applyReindex = React.useCallback(state => {
        if (!state) return
        if (state.running) {
          setRebuild({
            phase: 'running', done: state.done ?? 0, total: state.total ?? 0,
            error: '', startedAt: state.started_at || '', elapsedMs: 0,
          })
          return
        }
        if (state.error) {
          setRebuild({
            phase: 'error', done: state.done ?? 0, total: state.total ?? 0,
            error: state.error, startedAt: state.started_at || '',
            elapsedMs: state.elapsed_ms ?? 0,
          })
          return
        }
        if (state.done || state.total) {
          setRebuild({
            phase: 'done', done: state.done ?? 0, total: state.total ?? 0,
            error: '', startedAt: state.started_at || '',
            elapsedMs: state.elapsed_ms ?? 0,
          })
          return
        }
        // 空库：只有「刚才是 running」时才需要落一条完成提示
        setRebuild(prev => (prev?.phase === 'running'
          ? { phase: 'done', done: 0, total: 0, error: '', startedAt: '',
              elapsedMs: state.elapsed_ms ?? 0 }
          : prev))
      }, [])

      const load = React.useCallback(async () => {
        setError('')
        try {
          const response = await fetch('/longmemory/stats', { cache: 'no-store' })
          if (!response.ok) throw new Error(`HTTP ${response.status}`)
          const stats = await response.json()
          const source = {
            'model.jev': stats.models?.jev,
            'model.extract': stats.models?.extract,
            embedding: stats.models?.embedding,
          }
          const next = {}
          for (const key of Object.keys(MODEL_FALLBACK)) {
            next[key] = { ...MODEL_FALLBACK[key], ...(source[key] || {}) }
          }
          setSettings(next)
          setDirty({})
        } catch (e) {
          setError(String(e?.message ?? e))
        }
        applyReindex((await loadVec())?.reindex)
      }, [loadVec, applyReindex])

      React.useEffect(() => { void load() }, [load])

      // 重建是后台线程：点下去到 running=False 之前一直显示进度，读不到状态也不收起
      const reindexing = rebuild?.phase === 'running'
      React.useEffect(() => {
        if (!reindexing) return undefined
        let alive = true
        let failures = 0
        const tick = async () => {
          const status = await loadVec()
          if (!alive) return
          if (status?.reindex) {
            failures = 0
            if (status.reindex.running || startAck.current) {
              applyReindex(status.reindex)
            }
            return
          }
          failures += 1
          if (failures >= 8) {          // 约 12s 读不到状态：保留进度但报错
            setRebuild(prev => (prev && prev.phase === 'running'
              ? { ...prev, phase: 'error',
                  error: '状态读取失败（内核无响应），重建可能仍在后台进行' }
              : prev))
          }
        }
        void tick()
        const timer = setInterval(() => { void tick() }, 1500)
        return () => { alive = false; clearInterval(timer) }
      }, [reindexing, loadVec, applyReindex])

      const update = (key, name, value) => {
        setSettings(prev => ({ ...prev, [key]: { ...prev[key], [name]: value } }))
        setDirty(prev => ({ ...prev, [key]: true }))
        setStatus('')
      }

      const save = async key => {
        setStatus('')
        setError('')
        const value = { ...settings[key] }
        for (const name of ['timeoutMs', 'dim', 'batchSize']) {
          if (typeof value[name] === 'string') value[name] = Number(value[name]) || 0
        }
        try {
          const result = await callApi({ op: 'settings.set', key, value })
          setSettings(prev => ({
            ...prev, [key]: { ...MODEL_FALLBACK[key], ...(result.value || {}) },
          }))
          setDirty(prev => ({ ...prev, [key]: false }))
          return true
        } catch (e) {
          setError(String(e?.message ?? e))
          return false
        }
      }

      const saveAndTest = async key => {
        const ok = await save(key)
        if (!ok) return
        const kind = MODEL_KIND[key]
        setBusy(kind)
        try {
          const result = await postJson('/longmemory/test', { kind })
          setStatus(kind === 'embedding'
            ? `Embedding OK：${result.dim} 维 · ${result.count} 条 · ${result.ms ?? '?'}ms`
            : `文本模型 OK：${result.model} → ${result.sample || '（空）'} · ${result.ms ?? '?'}ms`)
        } catch (e) {
          setError(`测试失败：${String(e?.message ?? e)}`)
        } finally {
          setBusy('')
        }
      }

      const reindex = async () => {
        if (rebuild?.phase === 'running') return
        setStatus('')
        setError('')
        // 点下去立刻进入「重建中」，不等第一次轮询
        startAck.current = false
        setRebuild({ phase: 'running', done: 0, total: 0, error: '',
                     startedAt: new Date().toISOString(), elapsedMs: 0 })
        try {
          const result = await callApi({ op: 'vec.reindex' })
          startAck.current = true
          if (result?.state) applyReindex(result.state)
          await loadVec()
        } catch (e) {
          startAck.current = false
          setRebuild({ phase: 'error', done: 0, total: 0, startedAt: '',
                       elapsedMs: 0, error: String(e?.message ?? e) })
        }
      }

      /** 重建进度：running 一直显示到内核报 running=False 为止。 */
      const reindexStatus = () => {
        if (!rebuild) return null
        if (rebuild.phase === 'running') {
          const total = Number(rebuild.total) || 0
          const pct = total
            ? Math.min(100, Math.round((Number(rebuild.done) || 0) / total * 100))
            : 0
          const started = Date.parse(rebuild.startedAt)
          const elapsed = Number.isNaN(started)
            ? null
            : Math.max(0, Math.round((Date.now() - started) / 1000))
          return h('div', { style: { display: 'flex', flexDirection: 'column', gap: '4px' } },
            h('div', { style: S.cardLabel },
              `重建中 ${rebuild.done}${total ? `/${total}（${pct}%）` : ''}`
              + (elapsed === null ? '' : ` · 已用 ${elapsed}s`)),
            h('div', { style: S.barTrack },
              h('div', { style: { ...S.barFill, width: `${pct}%` } })))
        }
        if (rebuild.phase === 'error') {
          return h('div', { className: 'lm-error' },
            `重建失败：${rebuild.error || '未知错误'}`
            + (rebuild.total ? `（已完成 ${rebuild.done}/${rebuild.total}）` : ''))
        }
        return h('div', { style: S.cardLabel },
          rebuild.total
            ? `上次重建完成：${rebuild.done}/${rebuild.total}`
              + (rebuild.elapsedMs ? ` · ${(rebuild.elapsedMs / 1000).toFixed(1)}s` : '')
            : '上次重建完成：没有需要索引的条目')
      }

      const field = (key, name, label, options = {}) => h('label', {
        key: `${key}.${name}`, style: S.field,
      },
        h('span', { style: S.label }, label),
        h('input', {
          className: 'lm-input',
          style: options.narrow ? { width: '100%', minWidth: 0 } : { width: '100%' },
          type: options.type || 'text',
          value: settings[key][name] ?? '',
          placeholder: options.placeholder || '',
          onChange: e => update(key, name, e.target.value),
        }))

      if (!settings) {
        return h('div', { style: S.cardLabel },
          error ? `读取设置失败：${error}` : '读取模型设置…')
      }

      const llmCard = ([key, title, hint]) => {
        const custom = settings[key].mode === 'custom'
        return h('div', {
          key, className: 'lm-card',
          style: { display: 'flex', flexDirection: 'column', gap: '8px' },
        },
          h('div', { style: { display: 'flex', alignItems: 'baseline', gap: '8px' } },
            h('strong', { style: { fontSize: '13px' } }, title),
            dirty[key] && h('span', { style: S.cardLabel }, '未保存')),
          h('div', { style: S.cardLabel }, hint),
          h('label', { key: `${key}.mode`, style: S.field },
            h('span', { style: S.label }, '调用通道'),
            h('select', {
              className: 'lm-select',
              style: { width: '100%' },
              value: settings[key].mode || 'host',
              onChange: e => update(key, 'mode', e.target.value),
            },
              h('option', { key: 'host', value: 'host' }, '宿主模型（ctx.llm）'),
              h('option', { key: 'custom', value: 'custom' }, '自定义端点（OpenAI 兼容）'))),
          custom
            ? h('div', null,
                field(key, 'baseUrl', 'baseUrl', { placeholder: 'https://api.example.com/v1' }),
                field(key, 'model', '模型名', { placeholder: 'deepseek-v4.1-flash' }),
                field(key, 'apiKey', 'API Key（留空保持原值）', { type: 'password' }))
            : h('div', null,
                field(key, 'provider', 'provider', { placeholder: 'workbuddy' }),
                field(key, 'model', '模型名', { placeholder: 'deepseek-v4.1-flash' })),
          field(key, 'timeoutMs', '超时（毫秒）', { narrow: true }),
          h('div', { style: S.bar },
            Btn({
              variant: 'primary', disabled: Boolean(busy),
              onClick: () => { void saveAndTest(key) },
            }, busy === MODEL_KIND[key] ? '测试中…' : '保存并测试')))
      }

      const embeddingCard = () => {
        const key = 'embedding'
        const counts = vec?.counts || {}
        return h('div', {
          key, className: 'lm-card',
          style: { display: 'flex', flexDirection: 'column', gap: '8px' },
        },
          h('div', { style: { display: 'flex', alignItems: 'baseline', gap: '8px' } },
            h('strong', { style: { fontSize: '13px' } }, 'Embedding'),
            dirty[key] && h('span', { style: S.cardLabel }, '未保存')),
          h('div', { style: S.cardLabel },
            '检索向量化。默认本地 Ollama bge-m3（1024 维），改维度后需要重建索引。'),
          h('label', { key: `${key}.flavor`, style: S.field },
            h('span', { style: S.label }, '接口协议'),
            h('select', {
              className: 'lm-select',
              style: { width: '100%' },
              value: settings[key].flavor || 'openai',
              onChange: e => update(key, 'flavor', e.target.value),
            },
              h('option', { key: 'openai', value: 'openai' },
                'OpenAI 兼容（/v1/embeddings）'),
              h('option', { key: 'ollama', value: 'ollama' }, 'Ollama 原生（/api/embed）'))),
          field(key, 'baseUrl', 'baseUrl', { placeholder: 'http://127.0.0.1:11434/v1' }),
          field(key, 'model', '模型名', { placeholder: 'bge-m3' }),
          field(key, 'apiKey', 'API Key（留空保持原值）', { type: 'password' }),
          h('div', { style: S.bar },
            field(key, 'dim', '维度', { narrow: true }),
            field(key, 'batchSize', '批大小', { narrow: true }),
            field(key, 'timeoutMs', '超时（毫秒）', { narrow: true })),
          h('div', { style: S.cardLabel },
            vec
              ? `向量索引：${vec.enabled ? '可用' : '不可用（缺 sqlite-vec）'} · ${vec.dim || 0} 维 · `
                + annLabel(vec)
                + ` · 经验 ${counts.experience ?? 0} / 认知 ${counts.cognition ?? 0} / `
                + `洞见 ${counts.insight ?? 0}`
              : '向量索引状态读取中…'),
          reindexStatus(),
          h('div', { style: S.bar },
            Btn({
              variant: 'primary', disabled: Boolean(busy),
              onClick: () => { void saveAndTest(key) },
            }, busy === 'embedding' ? '测试中…' : '保存并测试'),
            Btn({
              disabled: reindexing,
              onClick: () => { void reindex() },
            }, reindexing ? '重建中…' : '重建向量索引')))
      }

      return h('div', { style: { display: 'flex', flexDirection: 'column', gap: '10px', minWidth: 0 } },
        h('div', { style: { display: 'grid', gap: '12px',
                            gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))' } },
          ...MODEL_CARDS.map(llmCard),
          embeddingCard()),
        h('div', { style: S.bar },
          Btn({ onClick: () => { void load() } }, '重新读取'),
          status && h('span', { style: S.cardLabel }, status)),
        error && h('div', { className: 'lm-error' }, error),
        h('div', { style: { ...S.cardLabel, maxWidth: '760px' } },
          '设置存在本地记忆库 app_setting 表；API Key 只在本机进程间使用，'
          + '页面上永远显示为掩码，留空或提交掩码都表示保持原值。'))
    }

    function MemorySettings({ openBrowser } = {}) {
      const [tab, setTab] = React.useState('overview')
      const [schema, setSchema] = React.useState(null)
      const [error, setError] = React.useState('')

      React.useEffect(() => {
        let alive = true
        callApi({ op: 'tables' })
          .then(result => { if (alive) setSchema(result) })
          .catch(e => { if (alive) setError(String(e?.message ?? e)) })
        return () => { alive = false }
      }, [])

      const tableSchema = name => schema?.tables?.find(item => item.name === name)
      const dataTab = tab !== 'overview' && tab !== 'logs' && tab !== 'models'
        && tab !== 'self_configured' && tab !== 'self_notes'

      return h('section', { className: 'lm-root', style: S.wrap },
        h('div', { style: S.head },
          h('h2', { style: S.title }, '长期记忆'),
          Btn({ onClick: () => openBrowser?.() }, '在右侧栏打开')),
        h('div', { className: 'lm-tabs' }, ...TABS.map(item =>
          Btn({
            key: item.id, variant: 'tab', active: tab === item.id,
            onClick: () => setTab(item.id),
          }, item.label))),
        error && h('div', { className: 'lm-error' }, `管理接口不可用：${error}`),
        tab === 'overview' && h(Overview),
        tab === 'models' && h(ModelSettings),
        tab === 'logs' && h(Logs),
        tab === 'self_configured' && h(SelfSettings, { mode: 'configured' }),
        tab === 'self_notes' && h(SelfSettings, { mode: 'agent' }),
        dataTab && (tableSchema(tab)
          ? h(DataTable, { table: tab, schema: tableSchema(tab) })
          : h('div', { style: S.cardLabel }, '读取表结构…')))
    }

    return {
      inject: ['slots', 'sidebarRight', 'sidebarRightTabs'],
      apply(ctx) {
        const openBrowser = () => {
          try {
            ctx.sidebarRight?.openTab(BROWSER_KIND)
          } catch {
            /* 没有可用会话时忽略 */
          }
        }
        ctx.effect(() => ctx.sidebarRightTabs.register({
          id: BROWSER_ID,
          kind: BROWSER_KIND,
          priority: 'builtin',
          title: () => '长期记忆',
        }), 'longmemory: browser tab type')
        ctx.slots.inject('sidebar.right.pane.tab', () => ctx.slots.register({
          name: 'sidebar.right.pane.tab',
          key: BROWSER_ID,
        }, MemoryBrowser))
        ctx.slots.inject('sidebar.right.pane.tab.title', () => ctx.slots.register({
          name: 'sidebar.right.pane.tab.title',
          key: BROWSER_ID,
        }, MemoryBrowserTitle))
        ctx.slots.inject('settings.section', () => ctx.slots.register({
          name: 'settings.section',
          id: 'longmemory',
          order: 40,
          label: () => '长期记忆',
          inject: () => ({ openBrowser }),
        }, MemorySettings))
        ctx.slots.inject('conversation.session.header.utilities', () => ctx.slots.register({
          name: 'conversation.session.header.utilities',
          id: BROWSER_ID,
          order: 30,
          label: () => '长期记忆',
          inject: () => ({ open: openBrowser }),
        }, MemoryHeaderButton))
      },
    }
  },
})
