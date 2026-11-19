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
const PAGES = ["novo", "aovivo", "fila", "clips", "publicar", "servidor"];

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
  const key = clips.map((c) => `${c.name}:${c.rating}:${c.favorite}`).join();
  $("#bulk-actions").hidden = clips.length === 0;
  $("#zip-favorites").hidden = !clips.some((c) => c.favorite);
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
    const star = el("button", { className: `rate star ${clip.favorite ? "active" : ""}`, textContent: clip.favorite ? "★" : "☆",
      ariaLabel: clip.favorite ? "Tirar dos favoritos" : "Favoritar: não apagar este clip" });
    star.setAttribute("aria-pressed", Boolean(clip.favorite));
    star.addEventListener("click", async () => {
      await api(`/api/clips/${clip.name}/favorite`, { method: "POST", json: { on: !clip.favorite } });
      refresh();
    });
    const publishButton = el("button", { className: "btn ghost small", textContent: "Publicar" });
    publishButton.addEventListener("click", () => openPublish(clip));
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
        ...(clip.expires_in != null ? [el("div", { className: "expires", id: `expires-${clip.name}`, textContent: expiryText(clip.expires_in) })]
          : clip.favorite ? [el("div", { className: "expires", textContent: "★ Favorito: não é apagado" })] : []),
        el("div", { className: "rating" },
          el("span", { textContent: "Ficou bom?" }),
          rate(true, "Clip bom", "👍"), rate(false, "Clip ruim", "👎"), star),
        el("div", { className: "actions" },
          el("a", { className: "btn primary small", href: `/media/${clip.name}.mp4?download=1`, textContent: "Baixar" }),
          publishButton, remove)));
  }));
}

function renderRatings(summary) {
  const total = summary.good + summary.bad;
  const cal = summary.calibration;
  let text = `Suas avaliações até agora: ${summary.good} 👍 · ${summary.bad} 👎. `;
  if (cal.calibrated) {
    text += cal.min_score > cal.default
      ? `Com base nelas, os clips automáticos ficaram mais exigentes (nota mínima ${cal.min_score}, era ${cal.default}).`
      : `Como quase tudo foi aprovado, os clips automáticos ficaram menos exigentes (nota mínima ${cal.min_score}, era ${cal.default}).`;
  } else if (cal.needed > 0) {
    text += `Faltam ${cal.needed} avaliações de clips automáticos para o sistema começar a se ajustar ao seu gosto.`;
  } else {
    text += "As avaliações confirmam a exigência atual dos clips automáticos.";
  }
  $("#ratings-summary").hidden = total === 0;
  $("#ratings-summary").textContent = text;
}

/* ---------- publicar ---------- */
let publishState = { platforms: [], posts: [] };
let publishClip = null;

function when(seconds) {
  return new Date(seconds * 1000).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}

function renderPublish(state) {
  publishState = state;
  const waiting = state.posts.filter((p) => Object.values(p.targets).some((t) => t.status === "agendado")).length;
  $("#posts-badge").hidden = waiting === 0;
  $("#posts-badge").textContent = waiting;

  $("#platforms").replaceChildren(...state.platforms.map((platform) => {
    const action = el("button", { className: "btn ghost small",
      textContent: platform.connected ? "Desconectar" : "Conectar" });
    action.disabled = !platform.configured && !platform.connected;
    action.addEventListener("click", async () => {
      if (platform.connected) {
        if (!confirm(`Desconectar ${platform.label}?`)) return;
        renderPublish(await api(`/api/publish/${platform.platform}/disconnect`, { method: "POST" }));
      } else {
        location.href = (await api(`/api/publish/${platform.platform}/connect`)).url;
      }
    });
    const detail = platform.connected ? `Conectado (${platform.account})`
      : platform.configured ? "Pronto para conectar"
        : `Falta configurar no servidor: ${platform.env.join(" e ")}`;
    return el("div", { className: "channel" },
      el("span", { className: `pill ${platform.connected ? "" : "off"}`, textContent: platform.connected ? "CONECTADO" : "DESLIGADO" }),
      el("span", { className: "channel-name" }, platform.label,
        el("small", { textContent: detail }),
        ...(platform.connected ? [] : [el("small", { className: "mono", textContent: `Redirect: ${platform.redirect}` })])),
      action);
  }));

  if (!state.posts.length) {
    $("#posts").replaceChildren(el("p", { className: "hint", textContent: "Nenhum post ainda." }));
    return;
  }
  $("#posts").replaceChildren(...state.posts.map((post) => {
    const targets = Object.entries(post.targets).map(([name, target]) => {
      const label = state.platforms.find((p) => p.platform === name)?.label || name;
      return el("small", { className: target.status === "erro" ? "err" : "" },
        `${label}: ${target.status}${target.detail ? ` · ${target.detail}` : ""}`);
    });
    const pending = Object.values(post.targets).some((t) => t.status === "agendado");
    const cancel = el("button", { className: "btn ghost small", textContent: pending ? "Cancelar" : "Tirar da lista" });
    cancel.addEventListener("click", async () => {
      renderPublish(await api(`/api/posts/${post.id}`, { method: "DELETE" }));
    });
    return el("div", { className: "channel" },
      el("span", { className: "channel-name" }, `${post.title || post.caption.slice(0, 40) || "Clip"} · ${when(post.when)}`, ...targets),
      cancel);
  }));
}

function openPublish(clip) {
  const connected = publishState.platforms.filter((p) => p.connected);
  if (!connected.length) {
    location.hash = "publicar";
    return;
  }
  publishClip = clip;
  $("#post-platforms").replaceChildren(...connected.map((platform) =>
    el("label", { className: "check" },
      el("input", { type: "checkbox", name: "platform", value: platform.platform, checked: true }),
      el("span", { textContent: platform.label }))));
  $("#form-post [name=caption]").value = clip.title || "";
  $("#form-post [name=when]").value = "";
  $("#form-post .form-error").hidden = true;
  $("#publish-dialog").showModal();
}

onSubmit($("#form-post"), async (data) => {
  const chosen = data.get("when");
  renderPublish(await api("/api/posts", { method: "POST", json: {
    clip: publishClip.name, platforms: data.getAll("platform"), caption: data.get("caption"),
    when: chosen ? new Date(chosen).getTime() / 1000 : null,
  } }));
  $("#publish-dialog").close();
  location.hash = "publicar";
}, "Enviando…");
$("#post-cancel").addEventListener("click", () => $("#publish-dialog").close());

const publishNote = new URLSearchParams(location.search).get("publicar");
if (publishNote) {
  $("#publish-message").textContent = publishNote === "conectado" ? "Conta conectada." : publishNote;
  $("#publish-message").classList.toggle("ok", publishNote === "conectado");
  $("#publish-message").hidden = false;
  history.replaceState(null, "", "/#publicar");
}

/* ---------- servidor ---------- */
function bytes(n) {
  return n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(1)} GB` : `${Math.round(n / 1024 ** 2)} MB`;
}

function span(seconds) {
  if (seconds == null) return "sem dados";
  return seconds >= 60 ? `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s` : `${Math.round(seconds)} s`;
}

function renderHealth(health) {
  $("#health-dot").hidden = health.warnings.length === 0;
  $("#health-warnings").replaceChildren(...health.warnings.map((text) =>
    el("div", { className: "alert", textContent: text })));
  const used = health.disk.total - health.disk.free;
  const tile = (label, value, detail, percent) => el("div", { className: "tile" },
    el("div", { className: "tile-label", textContent: label }),
    el("div", { className: "tile-value", textContent: value }),
    ...(percent == null ? [] : [el("div", { className: "meter" },
      el("div", { style: `width:${Math.min(100, percent)}%`, className: percent > 90 ? "hot" : "" }))]),
    el("div", { className: "hint", textContent: detail }));
  $("#health-tiles").replaceChildren(
    tile("Disco", `${bytes(health.disk.free)} livres`,
      `Clips: ${bytes(health.disk.clips)} · Trabalho e modelos: ${bytes(health.disk.work)}`, (used / health.disk.total) * 100),
    tile("Processador", `${health.cpu.percent}%`, `${health.cpu.cores} núcleos · carga ${health.cpu.load}`, health.cpu.percent),
    tile("Memória", health.memory.total ? `${bytes(health.memory.available)} livres` : "sem dados",
      health.memory.total ? `de ${bytes(health.memory.total)}` : "",
      health.memory.total ? (1 - health.memory.available / health.memory.total) * 100 : null),
    tile("Fila", `${health.queue.waiting} esperando`,
      health.queue.running ? `Agora: ${health.queue.running}` : "Nada processando agora"),
    tile("Lives", `${health.lives.active} de ${health.lives.max}`, "acompanhadas ao mesmo tempo"),
    tile("Tempo por clip", span(health.renders.average),
      health.renders.count ? `média dos últimos ${health.renders.count} · mais lento: ${span(health.renders.slowest)}` : "nenhum clip concluído ainda"),
    tile("No ar há", span(health.uptime), "desde o último reinício"));
  $("#health-errors").replaceChildren(...(health.errors.length
    ? health.errors.map((error) => el("div", { className: "channel" },
      el("span", { className: "channel-name" }, `${error.label} · ${when(error.when)}`,
        el("small", { className: "err", textContent: error.error }))))
    : [el("p", { className: "hint", textContent: "Nenhum erro registrado." })]));
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
    const [jobs, live, clips, publishing, health] = await Promise.all([
      api("/api/jobs"), api("/api/live"), api("/api/clips"), api("/api/publish"), api("/api/health")]);
    renderPublish(publishing);
    renderHealth(health);
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
