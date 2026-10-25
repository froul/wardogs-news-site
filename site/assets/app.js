// Wardogs News — сайт-лента: открывается на последней новости, листается вверх, как канал в Telegram.
(() => {
  const root = document.documentElement;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const posts = [...document.querySelectorAll(".post")];

  // ---------- открыть на последней новости (или на посте из ссылки #123) ----------
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";
  let touched = false; // человек сам начал листать — больше не дёргаем
  for (const ev of ["wheel", "touchstart", "keydown", "mousedown"]) {
    addEventListener(ev, () => { touched = true; }, { once: true, passive: true });
  }
  function target() {
    const id = decodeURIComponent(location.hash.slice(1));
    return id ? document.getElementById(id) : null;
  }
  function settle() {
    if (touched) return;
    const el = target();
    if (el) el.scrollIntoView({ block: "center" });
    else scrollTo(0, root.scrollHeight);
  }
  settle();
  // картинки и шрифты догружаются и меняют высоту — держимся за последний пост, пока не листают
  const pin = new ResizeObserver(() => (touched ? pin.disconnect() : settle()));
  pin.observe(document.getElementById("feed"));
  const lit = target();
  if (lit) { lit.classList.add("lit"); setTimeout(() => lit.classList.remove("lit"), 2500); }
  addEventListener("hashchange", () => {
    const el = target();
    if (!el) return;
    el.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
    el.classList.add("lit");
    setTimeout(() => el.classList.remove("lit"), 2500);
  });

  // ---------- появление постов и GIF только на экране ----------
  // по положению на экране при прокрутке: работает везде, пустой ленты не бывает
  let queued = false;
  function reveal() {
    queued = false;
    const h = innerHeight;
    for (const p of posts) {
      const r = p.getBoundingClientRect();
      const on = r.bottom > -80 && r.top < h + 80;
      if (on) p.classList.add("in");
      for (const v of p.querySelectorAll("video[data-src]")) {
        if (on) {
          if (!v.src) v.src = v.dataset.src;
          if (v.paused) v.play().catch(() => {});
        } else if (!v.paused) v.pause();
      }
    }
  }
  const queue = () => { if (!queued) { queued = true; requestAnimationFrame(reveal); } };
  addEventListener("scroll", queue, { passive: true });
  addEventListener("resize", queue);
  reveal();
  setTimeout(reveal, 300); // если вкладка открылась в фоне и кадры не рисуются

  // ---------- кнопка «к последней новости» ----------
  const down = document.getElementById("down");
  const onScroll = () => {
    const left = root.scrollHeight - (scrollY + innerHeight);
    down.classList.toggle("on", left > innerHeight * 0.8);
  };
  addEventListener("scroll", onScroll, { passive: true });
  onScroll();
  down.addEventListener("click", () => {
    if (location.hash) history.replaceState(null, "", location.pathname + location.search);
    scrollTo({ top: root.scrollHeight, behavior: reduce ? "auto" : "smooth" });
  });

  // ---------- спойлеры ----------
  document.addEventListener("click", (e) => {
    const s = e.target.closest(".spoiler");
    if (s) s.classList.add("shown");
  });

  // ---------- просмотр фото ----------
  const zoom = document.getElementById("zoom");
  const zimg = zoom.querySelector("img");
  let album = [], at = 0;
  function show(i) {
    at = (i + album.length) % album.length;
    zimg.src = album[at].getAttribute("href");
  }
  function close() {
    zoom.hidden = true;
    zimg.removeAttribute("src");
  }
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-zoom]");
    if (!a || e.ctrlKey || e.metaKey) return;
    e.preventDefault();
    album = [...a.closest(".media").querySelectorAll("a[data-zoom]")];
    zoom.classList.toggle("single", album.length < 2);
    show(album.indexOf(a));
    zoom.hidden = false;
  });
  zoom.addEventListener("click", (e) => {
    if (e.target.closest(".prev")) show(at - 1);
    else if (e.target.closest(".next")) show(at + 1);
    else close();
  });
  addEventListener("keydown", (e) => {
    if (zoom.hidden) return;
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft") show(at - 1);
    else if (e.key === "ArrowRight") show(at + 1);
  });

  // ---------- фон: красный растр (как в ленте для стрима) ----------
  const B8 = [0, 32, 8, 40, 2, 34, 10, 42, 48, 16, 56, 24, 50, 18, 58, 26, 12, 44, 4, 36, 14, 46, 6, 38,
    60, 28, 52, 20, 62, 30, 54, 22, 3, 35, 11, 43, 1, 33, 9, 41, 51, 19, 59, 27, 49, 17, 57, 25,
    15, 47, 7, 39, 13, 45, 5, 37, 63, 31, 55, 23, 61, 29, 53, 21].map((v) => (v + 0.5) / 64);
  const cv = document.getElementById("bg");
  const ctx = cv.getContext("2d");
  const SUB = 3;
  let cols = 0, rows = 0, img = null, buf = null;
  const pack = (r, g, b) => (255 << 24) | (b << 16) | (g << 8) | r;
  const C_BG = pack(5, 0, 2), C_DIM = pack(40, 0, 12), C_RED = pack(96, 0, 21);
  function size() {
    const w = Math.max(1, innerWidth), h = Math.max(1, innerHeight);
    const cell = Math.max(4, Math.round(Math.min(w, h) / 140));
    cols = Math.ceil(w / cell); rows = Math.ceil(h / cell);
    cv.width = cols * SUB; cv.height = rows * SUB;
    img = ctx.createImageData(cv.width, cv.height);
    buf = new Uint32Array(img.data.buffer);
  }
  function paint(t) {
    const s = t / 1000, asp = cols / rows;
    const blobs = [
      [0.5 + 0.45 * Math.sin(s * 0.11), 0.3 + 0.3 * Math.cos(s * 0.09), 0.45, 0.85],
      [0.5 + 0.5 * Math.cos(s * 0.07 + 2), 0.75 + 0.25 * Math.sin(s * 0.1 + 1), 0.38, 0.7],
    ];
    const scanY = ((s / 9) % 1) * 1.4 - 0.2;
    buf.fill(C_BG);
    for (let y = 0; y < rows; y++) {
      const v = y / rows;
      const scan = reduce ? 0 : 0.35 * Math.exp(-(((v - scanY) / 0.03) ** 2));
      for (let x = 0; x < cols; x++) {
        const u = x / cols;
        let L = 0;
        for (const [bx, by, r, a] of blobs) {
          const dx = (u - bx) * asp, dy = v - by;
          L += a * Math.exp(-(dx * dx + dy * dy) / (r * r));
        }
        const qv = (L * 0.42 + scan) * 2 + B8[(y & 7) * 8 + (x & 7)];
        if (qv < 1) continue;
        const col = qv >= 2 ? C_RED : C_DIM;
        const p = y * SUB * cv.width + x * SUB;
        buf[p] = col; buf[p + 1] = col; buf[p + cv.width] = col; buf[p + cv.width + 1] = col;
      }
    }
    ctx.putImageData(img, 0, 0);
  }
  size();
  addEventListener("resize", () => { size(); if (reduce) paint(0); });
  if (reduce) { paint(0); return; }
  let last = 0;
  (function draw(t) {
    requestAnimationFrame(draw);
    if (t - last < 80) return; // ~12 кадров/с хватает, бережём батарею телефона
    last = t;
    paint(t);
  })(0);
})();
