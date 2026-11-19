const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

async function api(path, options = {}) {
  if (options.json) {
    options.body = JSON.stringify(options.json);
    options.headers = { "Content-Type": "application/json" };
  }
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.startsWith("/api/login")) location.reload();
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? "Confira os campos preenchidos." : data.detail;
    throw new Error(detail || `Erro ${res.status}`);
  }
  return data;
}

/* ---------- navegação ---------- */
const PAGES = ["novo", "aovivo", "fila", "clips"];

function showPage() {
  const page = PAGES.includes(location.hash.slice(1)) ? location.hash.slice(1) : "novo";
  for (const p of PAGES) $(`#page-${p}`).hidden = p !== page;
  for (const a of $$(".sidebar nav a")) {
    const active = a.dataset.page === page;
    a.classList.toggle("active", active);
    active ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current");
  }
  setMenu(false);
  refresh();
}

function setMenu(open) {
  document.body.classList.toggle("menu-open", open);
  $("#scrim").hidden = !open;
  $("#menu-btn").setAttribute("aria-expanded", open);
}
$("#menu-btn").addEventListener("click", () => setMenu(!document.body.classList.contains("menu-open")));
$("#scrim").addEventListener("click", () => setMenu(false));
window.addEventListener("hashchange", showPage);

/* ---------- formulários ---------- */
for (const slot of $$(".shared-fields")) slot.append($("#shared-fields").content.cloneNode(true));

for (const tab of $$(".tab")) {
  tab.addEventListener("click", () => {
    for (const t of $$(".tab")) {
      t.classList.toggle("active", t === tab);
      t.setAttribute("aria-selected", t === tab);
    }
    for (const panel of $$("[data-tabpanel]")) panel.hidden = panel.dataset.tabpanel !== tab.dataset.tab;
  });
}

for (const radio of $$("#form-vod [name=mode]")) {
  radio.addEventListener("change", () => {
    const auto = $("#form-vod [name=mode]:checked").value === "auto";
    $("#range-fields").hidden = auto;
    $("#auto-fields").hidden = !auto;
  });
}

/** Liga o envio de um form: desabilita o botão, mostra erro no próprio form. */
function onSubmit(form, send, busyText) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("button[type=submit]", form);
    const error = $(".form-error", form);
    const idle = button.textContent;
    error.hidden = true;
    button.disabled = true;
    button.textContent = busyText;
    try {
      await send(new FormData(form));
    } catch (exc) {
      error.textContent = exc.message;
      error.hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = idle;
    }
  });
}

onSubmit($("#form-vod"), async (data) => {
  const body = { url: data.get("url"), title: data.get("title"), credit: data.get("credit"), cam: data.get("cam") };
  if (data.get("mode") === "auto") body.auto = Number(data.get("auto"));
  else Object.assign(body, { start: data.get("start"), end: data.get("end") });
  const result = await api("/api/clips/vod", { method: "POST", json: body });
  // Link de live que está no ar: o servidor passa a acompanhá-la em vez de baixar.
  location.hash = result.live ? "aovivo" : "fila";
}, "Conferindo o link…");

onSubmit($("#form-upload"), async (data) => {
  await api("/api/clips/upload", { method: "POST", body: data });
  $("#form-upload").reset();
  location.hash = "fila";
}, "Enviando vídeo…");

onSubmit($("#form-live-start"), async (data) => {
  renderLive(await api("/api/live/start", { method: "POST", json: { url: data.get("url"), auto: data.has("auto"), credit: data.get("credit") } }));
  $("#form-live-start").reset();
}, "Conectando…");

onSubmit($("#form-channel"), async (data) => {
  renderLive(await api("/api/channels", { method: "POST", json: { url: data.get("url"), credit: data.get("credit") } }));
  $("#form-channel").reset();
}, "Cadastrando…");

/* ---------- renderização ---------- */
function clock(seconds) {
  const s = Math.round(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

const liveCards = new Map();   // id da live -> card (criado uma vez, para não apagar o que foi digitado)

function liveCard(session) {
  const card = $("#live-card").content.firstElementChild.cloneNode(true);
  onSubmit($(".live-clip-form", card), async (data) => {
    await api(`/api/live/${session.id}/clip`, {
      method: "POST", json: { last: Number(data.get("last")), title: data.get("title") },
    });
    location.hash = "fila";
  }, "Cortando…");
  $(".live-stop", card).addEventListener("click", async () => {
    renderLive(await api(`/api/live/${session.id}/stop`, { method: "POST" }));
  });
  $(".live-remove", card).addEventListener("click", async () => {
    renderLive(await api(`/api/live/${session.id}`, { method: "DELETE" }));
  });
  return card;
}

function renderLive(live) {
  $("#live-dot").hidden = !live.sessions.some((s) => s.running);

  const seen = new Set();
  for (const session of live.sessions) {
    seen.add(session.id);
    if (!liveCards.has(session.id)) {
      liveCards.set(session.id, liveCard(session));
      $("#lives").append(liveCards.get(session.id));
    }
    const card = liveCards.get(session.id);
    const pill = $(".pill", card);
    pill.textContent = session.running ? "AO VIVO" : session.failed ? "FALHOU" : "ENCERRADA";
    pill.classList.toggle("off", !session.running);
    $(".live-name", card).textContent = session.name;
    $(".live-origin", card).hidden = session.origin !== "canal";
    $(".meter div", card).style.width = `${Math.min(100, (session.buffered / session.capacity) * 100)}%`;
    $(".live-buffer", card).textContent = session.failed
      ? "Não consegui gravar essa live. Confira se o link está certo e se ela está no ar."
      : session.buffered
        ? `${clock(session.buffered)} guardados (máximo ${clock(session.capacity)}).`
        : "Conectando à live; os primeiros segundos chegam em instantes.";
    const n = session.auto_clips;
    $(".live-auto", card).textContent = session.auto
      ? `Clip automático ligado${session.chat ? " (áudio + chat da Twitch)" : " (áudio)"} · ${n} clip${n === 1 ? "" : "s"} criado${n === 1 ? "" : "s"} nesta live.`
      : "Clip automático desligado.";
    $("button[type=submit]", card).hidden = session.buffered < 1;
    $(".live-stop", card).hidden = !session.running;
    $(".live-remove", card).hidden = session.running;
  }
  for (const [id, card] of liveCards) {
    if (!seen.has(id)) {
      card.remove();
      liveCards.delete(id);
    }
  }

  const rows = $("#channels");
  if (!live.channels.length) {
    rows.replaceChildren(el("p", { className: "hint", textContent: "Nenhum canal cadastrado ainda." }));
    return;
  }
  rows.replaceChildren(...live.channels.map((channel) => {
    const remove = el("button", { className: "btn ghost small", textContent: "Remover" });
    remove.addEventListener("click", async () => {
      if (!confirm(`Parar de acompanhar o canal ${channel.name}?`)) return;
      renderLive(await api("/api/channels/remove", { method: "POST", json: { url: channel.url } }));
    });
    return el("div", { className: "channel" },
      el("span", { className: `pill ${channel.live ? "" : "off"}`, textContent: channel.live ? "AO VIVO" : "OFFLINE" }),
      el("span", { className: "channel-name", textContent: channel.name, title: channel.url }),
      remove);
  }));
}

function renderJobs(jobs) {
  const pending = jobs.filter((j) => j.status === "fila" || j.status === "processando").length;
  $("#queue-badge").hidden = pending === 0;
  $("#queue-badge").textContent = pending;
  const list = $("#jobs");
  if (!jobs.length) {
    list.replaceChildren(el("div", { className: "empty" }, "Nada na fila. ",
      el("a", { href: "#novo", textContent: "Criar um clip" })));
    return;
  }
  list.replaceChildren(...jobs.map((job) => {
    const meta = el("div", { className: "job-meta" });
    if (job.status === "erro") {
      meta.classList.add("err");
      meta.textContent = job.error;
    } else if (job.status === "pronto") {
      meta.append(`${job.clips.length} clip${job.clips.length === 1 ? "" : "s"} · `,
        el("a", { href: "#clips", textContent: "ver em Meus clips", style: "color:var(--pink)" }));
    } else {
      meta.textContent = `${job.kind} · ${job.step}`;
    }
    return el("div", { className: "job" },
      el("div", { className: "job-label", textContent: job.label, title: job.label }),
      el("span", { className: `status ${job.status}`, textContent: job.status }),
      meta);
  }));
}

let clipsKey = "";
function expiryText(seconds) {
  if (seconds == null) return "";
  if (seconds < 60) return "Apaga em menos de 1 min";
  return `Apaga em ${Math.ceil(seconds / 60)} min`;
}

function renderClips(clips) {
  // A contagem regressiva muda a cada ciclo; o resto só é refeito se a lista mudou.
  for (const clip of clips) {
    const tag = document.getElementById(`expires-${clip.name}`);
    if (tag) {
      tag.textContent = expiryText(clip.expires_in);
      tag.classList.toggle("soon", clip.expires_in != null && clip.expires_in < 120);
    }
  }
  const key = clips.map((c) => `${c.name}:${c.rating}`).join();
  if (key === clipsKey) return;
  clipsKey = key;
  const grid = $("#clips");
  if (!clips.length) {
    grid.replaceChildren(el("div", { className: "empty" }, "Nenhum clip por aqui. ",
      el("a", { href: "#novo", textContent: "Criar um clip" })));
    return;
  }
  grid.replaceChildren(...clips.map((clip) => {
    const title = clip.title;  // sem título, o card não mostra nada no lugar
    const thumb = el("button", { className: "thumb", ariaLabel: title ? `Assistir: ${title}` : "Assistir clip" },
      el("img", { src: `/media/${clip.name}.jpg`, alt: "", loading: "lazy" }),
      el("span", { className: "dur", textContent: clock(clip.duration) }));
    thumb.addEventListener("click", () => {
      $("#player video").src = `/media/${clip.name}.mp4`;
      $("#player").showModal();
      $("#player video").play();
    });
    const remove = el("button", { className: "btn ghost small", textContent: "Excluir" });
    remove.addEventListener("click", async () => {
      if (!confirm(title ? `Excluir o clip "${title}"?` : "Excluir este clip?")) return;
      await api(`/api/clips/${clip.name}`, { method: "DELETE" });
      refresh();
    });
    const rate = (good, label, text) => {
      const active = clip.rating === (good ? "bom" : "ruim");
      const button = el("button", { className: `rate ${active ? "active" : ""}`, textContent: text, ariaLabel: label });
      button.setAttribute("aria-pressed", active);
      button.addEventListener("click", async () => {
        renderRatings(await api(`/api/clips/${clip.name}/rating`, { method: "POST", json: { good } }));
        refresh();
      });
      return button;
    };
    return el("article", { className: "clip" }, thumb,
      el("div", { className: "clip-body" },
        ...(clip.kind ? [el("span", { className: `kind ${clip.kind === "engraçado" ? "funny" : "impact"}`,
          textContent: clip.kind === "engraçado" ? "😂 Engraçado" : "💥 Impactante" })] : []),
        ...(title ? [el("div", { className: "clip-title", textContent: title })] : []),
        ...(clip.expires_in != null ? [el("div", { className: "expires", id: `expires-${clip.name}`, textContent: expiryText(clip.expires_in) })] : []),
        el("div", { className: "rating" },
          el("span", { textContent: "Ficou bom?" }),
          rate(true, "Clip bom", "👍"), rate(false, "Clip ruim", "👎")),
        el("div", { className: "actions" },
          el("a", { className: "btn primary small", href: `/media/${clip.name}.mp4?download=1`, textContent: "Baixar" }),
          remove)));
  }));
}

function renderRatings(summary) {
  const total = summary.good + summary.bad;
  $("#ratings-summary").hidden = total === 0;
  $("#ratings-summary").textContent =
    `Suas avaliações até agora: ${summary.good} 👍 · ${summary.bad} 👎. Elas ficam guardadas para ajustar a escolha dos momentos.`;
}

function closePlayer() {
  $("#player video").pause();
  $("#player").close();
}
$("#player-close").addEventListener("click", closePlayer);
$("#player").addEventListener("click", (event) => { if (event.target === $("#player")) closePlayer(); });

/* ---------- atualização periódica ---------- */
async function refresh() {
  try {
    const [jobs, live, clips] = await Promise.all([api("/api/jobs"), api("/api/live"), api("/api/clips")]);
    renderJobs(jobs);
    renderLive(live);
    renderClips(clips);
  } catch {
    /* servidor fora do ar: tenta de novo no próximo ciclo */
  }
}

api("/api/session").then((session) => {
  $("#logout").hidden = !session.auth_required;
  for (const node of $$(".ai-title")) node.hidden = !session.ai_titles;
  if (session.clip_ttl_minutes) {
    $("#clips-lead").textContent =
      `Baixe logo: cada clip é apagado ${session.clip_ttl_minutes} ${session.clip_ttl_minutes === 1 ? "minuto" : "minutos"} depois de ficar pronto.`;
  }
});
api("/api/ratings").then(renderRatings).catch(() => {});
$("#logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  location.reload();
});

/* ---------- app instalado ---------- */
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

/* ---------- avisos no celular ---------- */
function keyBytes(base64url) {
  const text = atob(base64url.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(text, (ch) => ch.charCodeAt(0));
}

async function setupPush() {
  const button = $("#push-btn");
  const note = $("#push-note");
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    note.textContent = "Para receber avisos no iPhone, instale o app na Tela de Início.";
    note.hidden = !/iPhone|iPad/.test(navigator.userAgent);
    return;
  }
  const registration = await navigator.serviceWorker.ready;
  const label = async () => {
    const on = await registration.pushManager.getSubscription();
    button.textContent = on ? "🔔 Avisos ativados" : "🔔 Ativar avisos";
    button.classList.toggle("on", Boolean(on));
  };
  button.hidden = false;
  await label();
  button.addEventListener("click", async () => {
    note.hidden = true;
    try {
      const current = await registration.pushManager.getSubscription();
      if (current) {
        await api("/api/push/unsubscribe", { method: "POST", json: { endpoint: current.endpoint } });
        await current.unsubscribe();
      } else {
        if ((await Notification.requestPermission()) !== "granted") {
          throw new Error("Os avisos estão bloqueados nas permissões do navegador.");
        }
        const { key } = await api("/api/push/key");
        const created = await registration.pushManager.subscribe({
          userVisibleOnly: true, applicationServerKey: keyBytes(key),
        });
        await api("/api/push/subscribe", { method: "POST", json: created.toJSON() });
      }
    } catch (exc) {
      note.textContent = exc.message || "Não deu para ativar os avisos neste navegador.";
      note.hidden = false;
    }
    await label();
  });
}
setupPush().catch(() => {});

// "Compartilhar" de outro app (Twitch, YouTube...) abre aqui com o link já preenchido.
const shared = new URLSearchParams(location.search);
const sharedLink = [shared.get("url"), shared.get("text"), shared.get("title")]
  .map((value) => (value || "").match(/https?:\/\/\S+/))
  .find(Boolean);
if (sharedLink) {
  $("#form-vod [name=url]").value = sharedLink[0];
  history.replaceState(null, "", "/#novo");
}

showPage();
setInterval(refresh, 2000);
