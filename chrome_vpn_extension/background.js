chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "FETCH_FB_REEL_HTML") {
    const reelId = msg.video_id;
    const targetUrl = `https://www.facebook.com/reel/${reelId}`;
    fetch(targetUrl, {
      method: "GET",
      credentials: "include",
      headers: {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
      }
    })
      .then(r => r.text())
      .then(html => sendResponse({ ok: true, html }))
      .catch(err => sendResponse({ ok: false, error: String(err) }));
    return true;
  }
});

