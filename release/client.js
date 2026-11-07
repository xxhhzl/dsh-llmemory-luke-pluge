/**
 * 浏览器侧：设置 → 「长期记忆」。
 *
 * 区域：
 *   概况  统计 + 库路径 + 最近事件
 *   数据  事件 / 经验 / 认知 / 消息 / 引用 / 工作记忆 的通用表格（增删查改）
 *         列与可编辑字段来自内核 `op:'tables'`，新增表不需要改前端
 *   日志  shell.log / kernel.stderr.log 尾部，可自动刷新
 *
 * Host 接口（仅回环）：
 *   GET  /longmemory/stats
 *   POST /longmemory/api   {op: tables|list|get|insert|update|delete, ...}
 *   GET  /longmemory/logs?source=shell|kernel&lines=N
 */
window.__ModuleLoader__.load({
  id: '@local/dsh-longmemory',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    const TABS = [
      { id: 'overview', label: '概况' },
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

    const SELF_KINDS = [
      ['identity', '身份', '我是谁、我的定位'],
      ['behavior', '行为准则', '我该怎么做（工作方式）'],
      ['limitation', '能力边界', '我不能做什么、何时需要用户确认'],
      ['goal', '目标', '我长期要达成什么'],
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

    function MemorySettings() {
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
      const dataTab = tab !== 'overview' && tab !== 'logs'
        && tab !== 'self_configured' && tab !== 'self_notes'

      return h('section', { style: S.wrap },
        h('div', { style: S.head }, h('h2', { style: S.title }, '长期记忆')),
        h('div', { style: S.tabs }, ...TABS.map(item =>
          h('button', {
            key: item.id, type: 'button',
            style: { ...S.tab, ...(tab === item.id ? S.tabActive : {}) },
            onClick: () => setTab(item.id),
          }, item.label))),
        error && h('div', { style: S.error }, `管理接口不可用：${error}`),
        tab === 'overview' && h(Overview),
        tab === 'logs' && h(Logs),
        tab === 'self_configured' && h(SelfSettings, { mode: 'configured' }),
        tab === 'self_notes' && h(SelfSettings, { mode: 'agent' }),
        dataTab && (tableSchema(tab)
          ? h(DataTable, { table: tab, schema: tableSchema(tab) })
          : h('div', { style: S.cardLabel }, '读取表结构…')))
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('settings.section', () => ctx.slots.register({
          name: 'settings.section',
          id: 'longmemory',
          order: 40,
          label: () => '长期记忆',
        }, MemorySettings))
      },
    }
  },
})
