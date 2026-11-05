window.addEventListener("DOMContentLoaded", () => {
  document.documentElement.setAttribute("data-reels-vpn-bridge", "installed");
});

window.addEventListener("message", async (event) => {
  if (!event.data || event.data.source !== "REELS_STUDIO_PAGE" || event.data.type !== "FETCH_FB_REEL_HTML") {
    return;
  }
  const { reqId, video_id } = event.data;
  try {
    const resp = await chrome.runtime.sendMessage({ type: "FETCH_FB_REEL_HTML", video_id });
    window.postMessage({ source: "REELS_VPN_EXTENSION", reqId, ...resp }, "*");
  } catch (e) {
    window.postMessage({ source: "REELS_VPN_EXTENSION", reqId, ok: false, error: String(e) }, "*");
  }
});

