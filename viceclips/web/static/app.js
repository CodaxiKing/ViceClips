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
  await api("/api/clips/vod", { method: "POST", json: body });
  location.hash = "fila";
}, "Enviando…");

onSubmit($("#form-upload"), async (data) => {
  await api("/api/clips/upload", { method: "POST", body: data });
  $("#form-upload").reset();
  location.hash = "fila";
}, "Enviando vídeo…");

onSubmit($("#form-live-start"), async (data) => {
  renderLive(await api("/api/live/start", { method: "POST", json: { url: data.get("url"), auto: data.has("auto"), credit: data.get("credit") } }));
}, "Conectando…");

onSubmit($("#form-live-clip"), async (data) => {
  await api("/api/live/clip", {
    method: "POST",
    json: { last: Number(data.get("last")), title: data.get("title"), credit: data.get("credit"), cam: data.get("cam") },
  });
  location.hash = "fila";
}, "Cortando…");

$("#live-stop").addEventListener("click", async () => {
  renderLive(await api("/api/live/stop", { method: "POST" }));
});

/* ---------- renderização ---------- */
function clock(seconds) {
  const s = Math.round(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function renderLive(live) {
  const hasBuffer = live.url && (live.running || live.buffered > 0);
  $("#form-live-start").hidden = live.running;
  $("#live-panel").hidden = !hasBuffer;
  $("#live-dot").hidden = !live.running;
  if (live.failed) {
    const error = $("#form-live-start .form-error");
    error.textContent = "Não consegui gravar essa live. Confira se o link está certo e se ela está no ar.";
    error.hidden = false;
  }
  if (!hasBuffer) return;
  $("#live-pill").textContent = live.running ? "AO VIVO" : "PARADA";
  $("#live-pill").classList.toggle("off", !live.running);
  $("#live-url").textContent = live.url;
  $("#live-meter").style.width = `${Math.min(100, (live.buffered / live.capacity) * 100)}%`;
  $("#live-buffer").textContent = live.buffered
    ? `${clock(live.buffered)} guardados (máximo ${clock(live.capacity)}).`
    : "Conectando à live; os primeiros segundos chegam em instantes.";
  const n = live.auto_clips;
  $("#live-auto").textContent = live.auto
    ? `Clip automático ligado · ${n} clip${n === 1 ? "" : "s"} criado${n === 1 ? "" : "s"} nesta live.`
    : "Clip automático desligado.";
  $("#live-stop").hidden = !live.running;
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
function renderClips(clips) {
  const key = clips.map((c) => c.name).join();
  if (key === clipsKey) return;
  clipsKey = key;
  const grid = $("#clips");
  if (!clips.length) {
    grid.replaceChildren(el("div", { className: "empty" }, "Nenhum clip ainda. ",
      el("a", { href: "#novo", textContent: "Criar o primeiro" })));
    return;
  }
  grid.replaceChildren(...clips.map((clip) => {
    const title = clip.title || "Sem título";
    const thumb = el("button", { className: "thumb", ariaLabel: `Assistir: ${title}` },
      el("img", { src: `/media/${clip.name}.jpg`, alt: "", loading: "lazy" }),
      el("span", { className: "dur", textContent: clock(clip.duration) }));
    thumb.addEventListener("click", () => {
      $("#player video").src = `/media/${clip.name}.mp4`;
      $("#player").showModal();
      $("#player video").play();
    });
    const remove = el("button", { className: "btn ghost small", textContent: "Excluir" });
    remove.addEventListener("click", async () => {
      if (!confirm(`Excluir o clip "${title}"?`)) return;
      await api(`/api/clips/${clip.name}`, { method: "DELETE" });
      refresh();
    });
    return el("article", { className: "clip" }, thumb,
      el("div", { className: "clip-body" },
        el("div", { className: "clip-title", textContent: title }),
        el("div", { className: "actions" },
          el("a", { className: "btn primary small", href: `/media/${clip.name}.mp4?download=1`, textContent: "Baixar" }),
          remove)));
  }));
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
});
$("#logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  location.reload();
});

showPage();
setInterval(refresh, 2000);
