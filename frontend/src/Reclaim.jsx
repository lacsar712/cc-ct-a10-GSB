import { createSignal, onCleanup, onMount, Show, For } from "solid-js";
import {
  fetchHoldConfig,
  updateHoldConfig,
  fetchHolds,
  fetchReclamations,
  fetchReconcile,
  simulateHold,
  triggerReclaim,
} from "./api";

function fmtClock(iso) {
  return iso ? new Date(iso).toLocaleTimeString() : "—";
}

function fmtRemain(sec) {
  if (sec === null || sec === undefined) return "—";
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return m > 0 ? `${m}分${String(s).padStart(2, "0")}秒` : `${s} 秒`;
}

function Reclaim(props) {
  const canWrite = () => !!props.user?.can_write;

  const [cfg, setCfg] = createSignal(null);
  const [holds, setHolds] = createSignal([]);
  const [ledger, setLedger] = createSignal([]);
  const [rec, setRec] = createSignal(null);
  const [err, setErr] = createSignal("");
  const [saving, setSaving] = createSignal(false);
  const [busy, setBusy] = createSignal(false);

  const [secInput, setSecInput] = createSignal("300");
  const [toolCode, setToolCode] = createSignal("");
  const [offsetUm, setOffsetUm] = createSignal("");
  let secInputPrimed = false;

  // 本地每秒走一次倒计时，数据每 2 秒向后台拉取对账
  const [tick, setTick] = createSignal(0);

  async function loadAll() {
    try {
      const [c, h, l, r] = await Promise.all([
        fetchHoldConfig(),
        fetchHolds(),
        fetchReclamations(),
        fetchReconcile(),
      ]);
      setCfg(c);
      setHolds(h);
      setLedger(l);
      setRec(r);
      if (!secInputPrimed) {
        setSecInput(String(c.max_hold_seconds));
        secInputPrimed = true;
      }
      setErr("");
    } catch (e) {
      setErr(e.message);
    }
  }

  onMount(() => {
    loadAll();
    const dataTimer = setInterval(loadAll, 2000);
    const tickTimer = setInterval(() => setTick((n) => n + 1), 1000);
    onCleanup(() => {
      clearInterval(dataTimer);
      clearInterval(tickTimer);
    });
  });

  // 依据服务器占入时间与本地时钟实时算剩余秒数
  function liveRemaining(h) {
    if (!h.claimed_at || h.hold_seconds === null) return { sec: null, expired: false, overdue: 0 };
    const deadline = new Date(h.claimed_at).getTime() + h.hold_seconds * 1000;
    const ms = deadline - Date.now();
    return {
      sec: Math.max(0, Math.ceil(ms / 1000)),
      expired: ms <= 0,
      overdue: ms <= 0 ? Math.ceil(-ms / 1000) : 0,
    };
  }

  async function saveSec(e) {
    e.preventDefault();
    setSaving(true);
    setErr("");
    try {
      const c = await updateHoldConfig(secInput());
      setCfg(c);
    } catch (e2) {
      setErr(e2.message);
    } finally {
      setSaving(false);
    }
  }

  async function makeStuck(e) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await simulateHold(toolCode(), offsetUm());
      setToolCode("");
      setOffsetUm("");
      await loadAll();
    } catch (e2) {
      setErr(e2.message);
    } finally {
      setBusy(false);
    }
  }

  async function reclaim(e) {
    e?.preventDefault?.();
    setBusy(true);
    setErr("");
    try {
      await triggerReclaim();
      await loadAll();
    } catch (e2) {
      setErr(e2.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section class="reclaim-wrap">
      <div class="toolbar">
        <h2>占位回收台</h2>
        <Show when={rec()} fallback={<span class="hint">对账中…</span>}>
          {(r) => (
            <span class={r().consistent ? "badge ok" : "badge bad"}>
              {r().consistent
                ? `账实一致（复核中 ${r().processing_count} / 回收账 ${r().ledger_count}）`
                : `账实不符 ${r().issues.length} 条`}
            </span>
          )}
        </Show>
      </div>
      <p class="hint">
        单子进入复核中时按当时设置快照占位时限；改时长只对之后新进入的单生效，已占着的不追溯。
        超过时限仍未写出结论的单由系统自动吐回待复核并在回收账落一条。
      </p>

      <Show when={err()}>
        <div class="banner error">{err()}</div>
      </Show>

      <Show when={rec() && !rec().consistent}>
        <div class="banner error">
          <For each={rec()?.issues || []}>
            {(it) => <div>· #{it.submission_id ?? "?"} {it.tool_code || ""}：{it.message}</div>}
          </For>
        </div>
      </Show>

      <div class="reclaim-grid">
        {/* 一、时长设置 */}
        <div class="card reclaim-col">
          <h3>时长设置</h3>
          <Show when={cfg()} fallback={<p class="hint">加载中…</p>}>
            {(c) => (
              <>
                <p class="big-num">{c().max_hold_seconds} <small>秒</small></p>
                <p class="hint">
                  上次更新：{fmtClock(c().updated_at)}
                  {c().updated_by ? ` · ${c().updated_by}` : ""}
                </p>
                <Show
                  when={canWrite()}
                  fallback={<p class="hint">只读账号仅可查看，不能修改时长。</p>}
                >
                  <form onSubmit={saveSec} class="form">
                    <label>
                      最长占位（秒，≥1）
                      <input
                        type="number"
                        min="1"
                        value={secInput()}
                        onInput={(e) => setSecInput(e.currentTarget.value)}
                        required
                      />
                    </label>
                    <button type="submit" disabled={saving()}>
                      {saving() ? "保存中…" : "保存新时长"}
                    </button>
                    <p class="hint">保存后只作用于之后新进入复核中的单。</p>
                  </form>
                </Show>
              </>
            )}
          </Show>
        </div>

        {/* 二、占位监视 */}
        <div class="card reclaim-col">
          <div class="toolbar">
            <h3>占位监视</h3>
            <span class="hint">{holds().length} 条占中</span>
          </div>
          <Show when={canWrite()}>
            <form onSubmit={makeStuck} class="form stuck-form">
              <label>
                刀具
                <input
                  placeholder="如 T10"
                  value={toolCode()}
                  onInput={(e) => setToolCode(e.currentTarget.value)}
                  required
                />
              </label>
              <label>
                刀补 µm
                <input
                  type="number"
                  value={offsetUm()}
                  onInput={(e) => setOffsetUm(e.currentTarget.value)}
                  required
                />
              </label>
              <button type="submit" class="ghost" disabled={busy()} title="提交一条进入复核中却故意不写结论的单">
                占住不写结论
              </button>
            </form>
          </Show>
          <div class="scroll-box">
            <table>
              <thead>
                <tr>
                  <th>刀具</th>
                  <th>占入</th>
                  <th>时限</th>
                  <th>剩余</th>
                </tr>
              </thead>
              <tbody>
                <For each={holds()}>
                  {(h) => {
                    tick(); // 订阅每秒心跳，驱动剩余秒数实时递减
                    const live = liveRemaining(h);
                    return (
                      <tr class={live.expired ? "row-expired" : ""}>
                        <td>{h.tool_code}</td>
                        <td>{fmtClock(h.claimed_at)}</td>
                        <td>{h.hold_seconds ?? "—"}s</td>
                        <td class={live.expired ? "fail" : "pass"}>
                          {live.expired ? `已超时 ${live.overdue}s` : fmtRemain(live.sec)}
                        </td>
                      </tr>
                    );
                  }}
                </For>
              </tbody>
            </table>
            <Show when={!holds().length}>
              <p class="hint">当前没有复核中的占位</p>
            </Show>
          </div>
          <Show when={canWrite()}>
            <button type="button" class="ghost full" onClick={reclaim} disabled={busy()}>
              立即扫描回收
            </button>
          </Show>
        </div>

        {/* 三、回收账 */}
        <div class="card reclaim-col">
          <div class="toolbar">
            <h3>回收账</h3>
            <span class="hint">{ledger().length} 条记录</span>
          </div>
          <div class="scroll-box">
            <table>
              <thead>
                <tr>
                  <th>刀具</th>
                  <th>占入</th>
                  <th>时限</th>
                  <th>吐回</th>
                </tr>
              </thead>
              <tbody>
                <For each={ledger()}>
                  {(r) => (
                    <tr>
                      <td>#{r.submission_id} {r.tool_code}</td>
                      <td>{fmtClock(r.claimed_at)}</td>
                      <td>{r.hold_seconds}s</td>
                      <td class="fail">{fmtClock(r.released_at)}</td>
                    </tr>
                  )}
                </For>
              </tbody>
            </table>
            <Show when={!ledger().length}>
              <p class="hint">暂无回收记录</p>
            </Show>
          </div>
        </div>
      </div>
    </section>
  );
}

export default Reclaim;
