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
              maxWidth: '1000px', color: 'var(--dsw-alias-label-primary)' },
      head: { display: 'flex', alignItems: 'baseline', gap: '12px', flexWrap: 'wrap' },
      title: { margin: 0, fontSize: '18px', fontWeight: 600 },
      tabs: { display: 'flex', gap: '14px', flexWrap: 'wrap',
              borderBottom: '.5px solid var(--dsw-alias-border-l2)' },
      tab: { font: 'inherit', fontSize: '13px', cursor: 'pointer', background: 'none',
             border: 0, padding: '6px 1px 8px', color: 'var(--dsw-alias-label-tertiary)' },
      tabActive: { color: 'var(--dsw-alias-label-primary)',
                   borderBottom: '2px solid var(--dsw-alias-label-primary)' },
      bar: { display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' },
      input: { font: 'inherit', fontSize: '13px', padding: '4px 8px', minWidth: '160px',
               borderRadius: '6px', border: '.5px solid var(--dsw-alias-border-l2)',
               background: 'transparent', color: 'inherit' },
      button: { font: 'inherit', fontSize: '13px', cursor: 'pointer', padding: '4px 10px',
                borderRadius: '6px', border: '.5px solid var(--dsw-alias-border-l2)',
                background: 'transparent', color: 'inherit' },
      danger: { color: 'var(--dsw-alias-state-error-primary, #c0392b)' },
      table: { width: '100%', borderCollapse: 'collapse', fontSize: '12px' },
      th: { textAlign: 'left', padding: '4px 6px',
            borderBottom: '.5px solid var(--dsw-alias-border-l2)',
            color: 'var(--dsw-alias-label-tertiary)', fontWeight: 500 },
      td: { padding: '4px 6px', borderBottom: '.5px solid var(--dsw-alias-border-l1)',
            verticalAlign: 'top' },
      clip: { display: 'inline-block', maxWidth: '340px', overflow: 'hidden',
              textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'bottom' },
      cards: { display: 'grid', gap: '10px',
               gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))' },
      card: { border: '.5px solid var(--dsw-alias-border-l2)', borderRadius: '8px',
              padding: '10px 12px' },
      cardLabel: { fontSize: '12px', color: 'var(--dsw-alias-label-tertiary)' },
      cardValue: { fontSize: '20px', fontWeight: 600 },
      mono: { fontFamily: 'var(--dsw-font-mono, monospace)', fontSize: '12px',
              wordBreak: 'break-all', color: 'var(--dsw-alias-label-secondary)' },
      error: { color: 'var(--dsw-alias-state-error-primary, #c0392b)', fontSize: '13px' },
      pre: { fontFamily: 'var(--dsw-font-mono, monospace)', fontSize: '11px',
             lineHeight: 1.5, maxHeight: '420px', overflow: 'auto', margin: 0,
             padding: '8px', border: '.5px solid var(--dsw-alias-border-l2)',
             borderRadius: '8px', whiteSpace: 'pre-wrap', wordBreak: 'break-all' },
      modal: { position: 'fixed', inset: 0, background: 'rgba(0,0,0,.35)',
               display: 'flex', alignItems: 'center', justifyContent: 'center',
               zIndex: 50 },
      dialog: { background: '#fff', color: '#111', borderRadius: '10px', padding: '16px',
                width: 'min(680px, 92vw)', display: 'flex', flexDirection: 'column',
                gap: '10px', maxHeight: '82vh', overflow: 'auto' },
      field: { display: 'flex', flexDirection: 'column', gap: '2px' },
      label: { fontSize: '12px', color: '#666' },
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
          h('button', { style: S.button, type: 'button', onClick: load }, '刷新'),
          state.status === 'loading' && h('span', { style: S.cardLabel }, '读取中…'),
          state.status === 'error'
            && h('span', { style: S.error }, `内核不可用：${state.error}`)),
        s && h('div', { style: S.cards },
          ...[['消息', s.messages], ['事件', s.events], ['经验', s.experiences],
              ['认知', s.cognitions]].map(([label, value]) =>
            h('div', { key: label, style: S.card },
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
              : '（暂无）')))
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
      return h('div', { style: S.modal, role: 'dialog' },
        h('div', { style: S.dialog },
          h('h3', { style: { margin: 0, fontSize: '16px' } },
            `${mode === 'create' ? '新增' : '编辑'} ${table}`),
          ...writable.map(name => h('label', { key: name, style: S.field },
            h('span', { style: S.label },
              name, required(name) ? ' *' : '',
              columnOf(name).type ? `（${columnOf(name).type}）` : ''),
            h('input', {
              style: { ...S.input, color: '#111', background: '#fff', minWidth: '100%' },
              value: values[name] ?? '',
              placeholder: mode === 'create' ? '留空则用默认值' : '',
              onChange: e => setValues(prev => ({ ...prev, [name]: e.target.value })),
            }))),
          missing.length > 0 && h('div', { style: S.error },
            `必填：${missing.join('、')}`),
          h('div', { style: { display: 'flex', gap: '8px', justifyContent: 'flex-end' } },
            h('button', { style: { ...S.button, color: '#111' }, type: 'button',
                          onClick: onCancel }, '取消'),
            h('button', { style: { ...S.button, color: '#111' }, type: 'button',
                          disabled: missing.length > 0,
                          onClick: submit }, '保存'))))
    }

    function DataTable({ table, schema }) {
      const [rows, setRows] = React.useState([])
      const [total, setTotal] = React.useState(0)
      const [offset, setOffset] = React.useState(0)
      const [query, setQuery] = React.useState('')
      const [error, setError] = React.useState('')
      const [editing, setEditing] = React.useState(null)
      const limit = 20

      const columns = schema?.columns ?? []
      const sample = rows[0] ?? {}
      const preview = (PREVIEW[table] ?? []).filter(name =>
        name === '_rowid' || rows.length === 0 || name in sample)
      const shown = preview.length ? preview : columns.slice(0, 5).map(c => c.name)

      const load = React.useCallback(async (nextOffset, nextQuery) => {
        setError('')
        try {
          const result = await callApi({
            op: 'list', table, limit, offset: nextOffset, query: nextQuery,
          })
          setRows(result.rows)
          setTotal(result.total)
          setOffset(nextOffset)
        } catch (e) {
          setError(String(e?.message ?? e))
        }
      }, [table])

      React.useEffect(() => { void load(0, '') }, [table]) // eslint-disable-line

      const remove = async row => {
        const key = locator(table, schema, row)
        if (!window.confirm(`删除 ${table} #${key}？该操作不可撤销。`)) return
        try {
          await callApi({ op: 'delete', table, id: key })
          await load(offset, query)
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
          await load(mode === 'create' ? 0 : offset, query)
        } catch (e) {
          setError(String(e?.message ?? e))
        }
      }

      return h('div', { style: { display: 'flex', flexDirection: 'column', gap: '8px' } },
        h('div', { style: S.bar },
          h('input', { style: S.input, placeholder: '搜索…', value: query,
                       onChange: e => setQuery(e.target.value),
                       onKeyDown: e => { if (e.key === 'Enter') void load(0, query) } }),
          h('button', { style: S.button, type: 'button', onClick: () => load(0, query) },
            '搜索'),
          h('button', { style: S.button, type: 'button',
                        onClick: () => setEditing({ mode: 'create', values: {} }) },
            '新增'),
          h('span', { style: S.cardLabel }, `共 ${total} 条`)),
        error && h('div', { style: S.error }, error),
        h('table', { style: S.table },
          h('thead', null, h('tr', null,
            ...shown.map(name => h('th', { key: name, style: S.th }, name)),
            h('th', { key: '_ops', style: S.th }, '操作'))),
          h('tbody', null, ...rows.map((row, index) => h('tr', { key: index },
            ...shown.map(name => h('td', { key: name, style: S.td },
              h('span', { style: S.clip, title: String(row[name] ?? '') },
                clip(row[name])))),
            h('td', { key: '_ops', style: S.td },
              h('button', { style: S.button, type: 'button',
                            onClick: () => { void openEdit(row) } }, '编辑'),
              ' ',
              h('button', { style: { ...S.button, ...S.danger }, type: 'button',
                            onClick: () => remove(row) }, '删除')))))),
        h('div', { style: S.bar },
          h('button', { style: S.button, type: 'button', disabled: offset === 0,
                        onClick: () => load(Math.max(0, offset - limit), query) }, '上一页'),
          h('button', { style: S.button, type: 'button',
                        disabled: offset + limit >= total,
                        onClick: () => load(offset + limit, query) }, '下一页')),
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

      return h('div', { style: { display: 'flex', flexDirection: 'column', gap: '8px' } },
        h('div', { style: S.bar },
          ...[['shell', 'shell.log'], ['kernel', 'kernel.stderr.log']].map(([id, label]) =>
            h('button', {
              key: id, type: 'button',
              style: { ...S.button, ...(source === id ? { fontWeight: 600 } : {}) },
              onClick: () => setSource(id),
            }, label)),
          h('button', { style: S.button, type: 'button', onClick: load }, '刷新'),
          h('label', { style: { display: 'flex', gap: '4px', alignItems: 'center',
                                fontSize: '12px' } },
            h('input', { type: 'checkbox', checked: auto,
                         onChange: e => setAuto(e.target.checked) }),
            '每 5 秒自动刷新')),
        error && h('div', { style: S.error }, error),
        h('pre', { style: S.pre }, lines.join('\n')))
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
      const limit = 20

      const load = React.useCallback(async (nextOffset, nextQuery) => {
        try {
          const result = await callApi({
            op: 'list', table, limit, offset: nextOffset, query: nextQuery,
          })
          setRows(result.rows)
          setTotal(result.total)
          setOffset(nextOffset)
          setError('')
        } catch (e) {
          setError(String(e?.message ?? e))
        }
      }, [table])

      React.useEffect(() => { void load(0, '') }, [load]) // eslint-disable-line

      const openDetail = async row => {
        try {
          setDetail(await callApi({ op: 'get', table, id: row.id }))
          setError('')
        } catch (e) {
          setError(String(e?.message ?? e))
        }
      }

      return h('div', { style: { display: 'flex', flexDirection: 'column', gap: '8px',
                                 padding: '8px' } },
        h('div', { style: S.bar }, ...BROWSER_TABLES.map(([id, label]) =>
          h('button', {
            key: id, type: 'button',
            style: { ...S.button, ...(table === id ? { fontWeight: 600 } : {}) },
            onClick: () => setTable(id),
          }, label))),
        h('div', { style: S.bar },
          h('input', { style: S.input, placeholder: '搜索…', value: query,
                       onChange: e => setQuery(e.target.value),
                       onKeyDown: e => { if (e.key === 'Enter') void load(0, query) } }),
          h('button', { style: S.button, type: 'button', onClick: () => load(0, query) },
            '搜索'),
          h('span', { style: S.cardLabel }, `共 ${total} 条`)),
        error && h('div', { style: S.error }, error),
        h('div', { style: { display: 'flex', flexDirection: 'column', gap: '6px' } },
          ...rows.map(row => h('button', {
            key: row.id, type: 'button',
            style: { ...S.button, textAlign: 'left', display: 'block', width: '100%' },
            onClick: () => openDetail(row),
          }, h('div', { style: S.cardLabel }, `#${row.id} ${row.title ?? row.subject ?? row.kind ?? ''}`),
             h('div', { style: { fontSize: '12px' } }, clip(row.summary ?? row.content, 120))))),
        h('div', { style: S.bar },
          h('button', { style: S.button, type: 'button', disabled: offset === 0,
                        onClick: () => load(Math.max(0, offset - limit), query) }, '上一页'),
          h('button', { style: S.button, type: 'button',
                        disabled: offset + limit >= total,
                        onClick: () => load(offset + limit, query) }, '下一页')),
        detail && h('div', { style: S.modal, role: 'dialog' },
          h('div', { style: S.dialog },
            h('div', { style: S.bar },
              h('strong', null, `${table} #${detail.id ?? ''}`),
              h('button', { style: { ...S.button, marginLeft: 'auto' }, type: 'button',
                            onClick: () => setDetail(null) }, '关闭')),
            h('pre', { style: S.pre }, JSON.stringify(detail, null, 2)))))
    }

    function MemoryBrowserTitle() {
      return h('span', null, '长期记忆')
    }

    function MemoryHeaderButton({ open } = {}) {
      return h('button', {
        style: S.button, type: 'button', title: '打开长期记忆',
        onClick: () => open?.(),
      }, '记忆')
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

      return h('div', { style: { display: 'flex', flexDirection: 'column', gap: '10px' } },
        h('div', { style: { ...S.cardLabel, maxWidth: '760px' } },
          configured
            ? '「设置的自我」保存后进入系统提示词 <self_configured>；同类只保留最新一条，旧条目留作历史、可停用。'
            : '「模型自述」由模型通过 memory_self_write 写入，进入动态上下文 <self_notes>；你可以在这里停用。'),
        configured && h('div', { style: { display: 'grid', gap: '10px' } },
          ...SELF_KINDS.map(([kind, label, hint]) => h('div', { key: kind, style: S.field },
            h('span', { style: S.label }, `${label}（${hint}）`),
            h('input', {
              style: S.input,
              value: values[kind] ?? '',
              placeholder: hint,
              onChange: e => setValues(prev => ({ ...prev, [kind]: e.target.value })),
            }),
            h('button', { style: S.button, type: 'button', onClick: () => save(kind) },
              `保存${label}`)))),
        h('div', { style: S.bar },
          h('button', { style: S.button, type: 'button', onClick: showPreview },
            '预览注入文本'),
          h('button', { style: S.button, type: 'button', onClick: load }, '刷新'),
          status && h('span', { style: S.cardLabel }, status)),
        error && h('div', { style: S.error }, error),
        preview && h('div', null,
          h('div', { style: S.cardLabel },
            `系统提示词 ${preview.text?.length ?? 0} 字符 · 自述 ${preview.notes_text?.length ?? 0} 字符 · 版本 ${preview.version || '无'}`),
          h('pre', { style: S.pre }, preview.text || '（系统提示词段为空）'),
          h('pre', { style: S.pre }, preview.notes_text || '（自述为空）')),
        h('div', { style: S.cardLabel }, '历史'),
        h('table', { style: S.table },
          h('thead', null, h('tr', null,
            ...['id', 'kind', 'content', 'revision', 'status'].map(name =>
              h('th', { key: name, style: S.th }, name)),
            h('th', { key: '_ops', style: S.th }, '操作'))),
          h('tbody', null, ...items.map(item => h('tr', { key: item.id },
            ...['id', 'kind', 'content', 'revision', 'status'].map(name =>
              h('td', { key: name, style: S.td },
                h('span', { style: S.clip, title: String(item[name] ?? '') },
                  clip(item[name])))),
            h('td', { key: '_ops', style: S.td },
              item.status === 'active' && h('button', {
                style: { ...S.button, ...S.danger }, type: 'button',
                onClick: () => disable(item.id),
              }, '停用')))))))
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

      const loadVec = React.useCallback(async () => {
        try {
          setVec(await callApi({ op: 'vec.status' }))
        } catch {
          setVec(null)
        }
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
        await loadVec()
      }, [loadVec])

      React.useEffect(() => { void load() }, [load])

      // 重建索引是后台线程：跑着就轮询进度
      const reindexing = Boolean(vec?.reindex?.running)
      React.useEffect(() => {
        if (!reindexing) return undefined
        const timer = setInterval(() => { void loadVec() }, 2000)
        return () => clearInterval(timer)
      }, [reindexing, loadVec])

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
        setStatus('')
        setError('')
        try {
          const result = await callApi({ op: 'vec.reindex' })
          setStatus(result.started ? '开始重建向量索引…' : '索引重建已在运行')
          await loadVec()
        } catch (e) {
          setError(String(e?.message ?? e))
        }
      }

      const field = (key, name, label, options = {}) => h('label', {
        key: `${key}.${name}`, style: S.field,
      },
        h('span', { style: S.label }, label),
        h('input', {
          style: options.narrow ? { ...S.input, minWidth: '90px' } : S.input,
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
          key, style: { ...S.card, display: 'flex', flexDirection: 'column', gap: '8px' },
        },
          h('div', { style: { display: 'flex', alignItems: 'baseline', gap: '8px' } },
            h('strong', { style: { fontSize: '13px' } }, title),
            dirty[key] && h('span', { style: S.cardLabel }, '未保存')),
          h('div', { style: S.cardLabel }, hint),
          h('label', { key: `${key}.mode`, style: S.field },
            h('span', { style: S.label }, '调用通道'),
            h('select', {
              style: S.input,
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
            h('button', {
              style: S.button, type: 'button', disabled: Boolean(busy),
              onClick: () => { void saveAndTest(key) },
            }, busy === MODEL_KIND[key] ? '测试中…' : '保存并测试')))
      }

      const embeddingCard = () => {
        const key = 'embedding'
        const counts = vec?.counts || {}
        const progress = vec?.reindex || {}
        return h('div', {
          key, style: { ...S.card, display: 'flex', flexDirection: 'column', gap: '8px' },
        },
          h('div', { style: { display: 'flex', alignItems: 'baseline', gap: '8px' } },
            h('strong', { style: { fontSize: '13px' } }, 'Embedding'),
            dirty[key] && h('span', { style: S.cardLabel }, '未保存')),
          h('div', { style: S.cardLabel },
            '检索向量化。默认本地 Ollama bge-m3（1024 维），改维度后需要重建索引。'),
          h('label', { key: `${key}.flavor`, style: S.field },
            h('span', { style: S.label }, '接口协议'),
            h('select', {
              style: S.input,
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
                + `经验 ${counts.experience ?? 0} / 认知 ${counts.cognition ?? 0} / `
                + `洞见 ${counts.insight ?? 0}`
              : '向量索引状态读取中…'),
          progress.running && h('div', { style: S.cardLabel },
            `重建中 ${progress.done ?? 0}/${progress.total ?? 0}`),
          progress.error && h('div', { style: S.error }, `重建失败：${progress.error}`),
          h('div', { style: S.bar },
            h('button', {
              style: S.button, type: 'button', disabled: Boolean(busy),
              onClick: () => { void saveAndTest(key) },
            }, busy === 'embedding' ? '测试中…' : '保存并测试'),
            h('button', {
              style: S.button, type: 'button', disabled: reindexing,
              onClick: () => { void reindex() },
            }, reindexing ? '重建中…' : '重建向量索引')))
      }

      return h('div', { style: { display: 'flex', flexDirection: 'column', gap: '10px' } },
        h('div', { style: { display: 'grid', gap: '12px',
                            gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))' } },
          ...MODEL_CARDS.map(llmCard),
          embeddingCard()),
        h('div', { style: S.bar },
          h('button', { style: S.button, type: 'button', onClick: () => { void load() } },
            '重新读取'),
          status && h('span', { style: S.cardLabel }, status)),
        error && h('div', { style: S.error }, error),
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

      return h('section', { style: S.wrap },
        h('div', { style: S.head },
          h('h2', { style: S.title }, '长期记忆'),
          h('button', { style: S.button, type: 'button',
                        onClick: () => openBrowser?.() }, '在右侧栏打开')),
        h('div', { style: S.tabs }, ...TABS.map(item =>
          h('button', {
            key: item.id, type: 'button',
            style: { ...S.tab, ...(tab === item.id ? S.tabActive : {}) },
            onClick: () => setTab(item.id),
          }, item.label))),
        error && h('div', { style: S.error }, `管理接口不可用：${error}`),
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
