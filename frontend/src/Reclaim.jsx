import { createSignal, For, onCleanup, onMount, Show } from "solid-js";
import { fetchReclaimOverview, updateHoldLimit } from "./api";

function fmtClock(iso) {
  return iso ? new Date(iso).toLocaleTimeString() : "—";
}

function fmtSeconds(v) {
  if (v === null || v === undefined) return "—";
  return `${Number(v).toFixed(1)} 秒`;
}

function ReclaimView({ user }) {
  const [data, setData] = createSignal(null);
  const [error, setError] = createSignal("");
  const [saving, setSaving] = createSignal(false);
  const [limitInput, setLimitInput] = createSignal("");
  const [savedTip, setSavedTip] = createSignal(false);

  let timer = null;

  async function load() {
    try {
      const d = await fetchReclaimOverview();
      setData(d);
      setError("");
      // 首次加载把输入框同步成当前时长。
      if (limitInput() === "") setLimitInput(String(d.setting.hold_limit_seconds));
    } catch (e) {
      setError(e.message);
    }
  }

  async function saveLimit(e) {
    e.preventDefault();
    setError("");
    setSaving(true);
    setSavedTip(false);
    try {
      const s = await updateHoldLimit(limitInput());
      setLimitInput(String(s.hold_limit_seconds));
      setSavedTip(true);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  onMount(() => {
    load();
    timer = setInterval(load, 1000);
  });
  onCleanup(() => timer && clearInterval(timer));

  return (
    <section class="reclaim-wrap">
      <div class="card toolbar reclaim-head">
        <div>
          <h2 style={{ margin: "0 0 0.15rem" }}>占位回收台</h2>
          <p class="hint" style={{ margin: 0 }}>
            单子进入「复核中」即占位，超过最长占位时长仍未写出结论，系统自动吐回「待复核」并记一笔回收账。改时长只作用于此后新进入复核中的单，已占着的不追溯。
          </p>
        </div>
        <span class="poll-tag">每秒刷新</span>
      </div>

      <Show when={error()}>
        <div class="banner error">{error()}</div>
      </Show>

      <div class="reclaim-grid">
        {/* 一、时长设置 */}
        <div class="card reclaim-col">
          <h3>最长占位时长</h3>
          <Show when={data()} fallback={<p class="hint">加载中…</p>}>
            {(d) => (
              <>
                <div class="kv">
                  <span>当前时长</span>
                  <strong>{d().setting.hold_limit_seconds} 秒</strong>
                </div>
                <div class="kv">
                  <span>上次修改</span>
                  <span>
                    {d().setting.updated_at
                      ? new Date(d().setting.updated_at).toLocaleString()
                      : "默认（未改过）"}
                  </span>
                </div>
                <Show
                  when={user().can_write}
                  fallback={
                    <p class="hint readonly-note">
                      当前为只读账号，可查看时长设置，但不能修改。
                    </p>
                  }
                >
                  <form onSubmit={saveLimit} class="form">
                    <label>
                      新时长（秒，1–86400）
                      <input
                        type="number"
                        min="1"
                        max="86400"
                        value={limitInput()}
                        onInput={(e) => setLimitInput(e.currentTarget.value)}
                        required
                      />
                    </label>
                    <button type="submit" disabled={saving()}>
                      {saving() ? "保存中…" : "保存时长"}
                    </button>
                  </form>
                  <Show when={savedTip()}>
                    <p class="hint ok-tip">
                      已保存。该时长只对之后新进入复核中的单生效，已占单不追溯。
                    </p>
                  </Show>
                </Show>
              </>
            )}
          </Show>
        </div>

        {/* 二、占位监视 */}
        <div class="card reclaim-col">
          <h3>占位监视（{data()?.watching.length ?? 0}）</h3>
          <Show when={data()} fallback={<p class="hint">加载中…</p>}>
            {(d) => (
              <Show
                when={d().watching.length}
                fallback={<p class="hint">当前没有复核中（占位）的单。</p>}
              >
                <table class="reclaim-table">
                  <thead>
                    <tr>
                      <th>刀具</th>
                      <th>占位起</th>
                      <th>时限</th>
                      <th>已占位</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    <For each={d().watching}>
                      {(w) => (
                        <tr class={w.expired ? "overdue" : ""}>
                          <td>#{w.id} {w.tool_code}</td>
                          <td>{fmtClock(w.claimed_at)}</td>
                          <td>{w.claim_limit_seconds}s</td>
                          <td>{fmtSeconds(w.held_seconds)}</td>
                          <td>
                            {w.expired ? (
                              <span class="fail">已超时，待回收</span>
                            ) : (
                              <span class="pass">剩 {fmtSeconds(w.remaining_seconds)}</span>
                            )}
                          </td>
                        </tr>
                      )}
                    </For>
                  </tbody>
                </table>
              </Show>
            )}
          </Show>
        </div>

        {/* 三、回收账 + 对账 */}
        <div class="card reclaim-col">
          <h3>回收账（{data()?.ledger.length ?? 0}）</h3>
          <Show when={data()} fallback={<p class="hint">加载中…</p>}>
            {(d) => (
              <>
                <div class={"reconcile " + (d().reconcile.ok ? "ok" : "bad")}>
                  <strong>
                    {d().reconcile.ok ? "✓ 账实相符" : "✗ 账实不符"}
                  </strong>
                  <span class="hint">
                    复核中 {d().reconcile.processing_count} · 回收账{" "}
                    {d().reconcile.ledger_count}
                  </span>
                  <Show when={!d().reconcile.ok}>
                    <ul class="mismatch-list">
                      <For each={d().reconcile.mismatches}>
                        {(m) => <li>{m}</li>}
                      </For>
                    </ul>
                  </Show>
                </div>
                <Show
                  when={d().ledger.length}
                  fallback={<p class="hint">还没有被回收的单。</p>}
                >
                  <table class="reclaim-table">
                    <thead>
                      <tr>
                        <th>刀具</th>
                        <th>回收时间</th>
                        <th>占用/时限</th>
                      </tr>
                    </thead>
                    <tbody>
                      <For each={d().ledger}>
                        {(r) => (
                          <tr>
                            <td>#{r.submission_id} {r.tool_code}</td>
                            <td>{fmtClock(r.reclaimed_at)}</td>
                            <td>
                              {Number(r.held_seconds).toFixed(1)}s / {r.limit_seconds}s
                            </td>
                          </tr>
                        )}
                      </For>
                    </tbody>
                  </table>
                </Show>
              </>
            )}
          </Show>
        </div>
      </div>
    </section>
  );
}

export default ReclaimView;
