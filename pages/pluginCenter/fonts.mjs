/** 浏览器侧字体加载；与设置保存结果独立，不下载到插件目录。 */
export function mountFontControls({ apiGet }) {
  const root = document.documentElement;
  const status = document.getElementById("fontStatus");
  const retry = document.getElementById("fontRetryBtn");
  let generation = 0;
  let controller = null;
  let sheet = null;

  function show(state, message) {
    status.dataset.state = state;
    status.textContent = message;
    retry.hidden = state !== "error";
  }

  async function load() {
    const current = ++generation;
    controller?.abort();
    controller = new AbortController();
    const { signal } = controller;
    sheet?.remove();
    sheet = null;
    root.style.removeProperty("--font-sans");
    show("loading", "正在读取字体线路…");
    try {
      const config = await apiGet("config");
      if (current !== generation) return;
      if (!config.font_url) {
        show("system", "界面字体：系统字体");
        return;
      }
      const family = config.font_family;
      if (!["MiSans", "MiSans VF"].includes(family) || new URL(config.font_url).protocol !== "https:") {
        throw new Error("Unsupported font source");
      }
      show("loading", `正在加载 MiSans · ${config.font_label}`);
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.crossOrigin = "anonymous";
      link.referrerPolicy = "no-referrer";
      link.href = config.font_url;
      sheet = link;
      await new Promise((resolve, reject) => {
        let finished = false;
        function finish(error) {
          if (finished) return;
          finished = true;
          clearTimeout(timer);
          signal.removeEventListener("abort", abort);
          link.onload = link.onerror = null;
          if (error) reject(error);
          else resolve();
        }
        const abort = () => finish(new DOMException("Font load cancelled", "AbortError"));
        const timer = setTimeout(() => finish(new Error("Font load timed out")), 12000);
        signal.addEventListener("abort", abort, { once: true });
        link.onerror = () => finish(new Error("Font stylesheet failed"));
        link.onload = async () => {
          try {
            const faces = await Promise.all([400, 500, 600, 700].map(weight =>
              document.fonts.load(`${weight} 14px "${family}"`, "插件配置安全分组字体MiSans0123456789")));
            if (faces.some(items => !items.length || items.some(face => face.status !== "loaded"))) {
              throw new Error("Font face unavailable");
            }
            finish();
          } catch (error) { finish(error); }
        };
        document.head.append(link);
      });
      if (current !== generation) return;
      root.style.setProperty("--font-sans", `"${family}", var(--font-system)`);
      show("ready", `界面字体：MiSans · ${config.font_label}`);
    } catch {
      // 被新线路替代的请求不能覆盖新的状态或字体。
      if (current !== generation) return;
      sheet?.remove();
      sheet = null;
      root.style.removeProperty("--font-sans");
      show("error", "MiSans 加载失败，当前使用系统字体。可重试或更换线路。");
    }
  }

  retry.addEventListener("click", load);
  return { load };
}
