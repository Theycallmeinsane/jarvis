const state = document.getElementById("state");
const port = document.getElementById("port");

async function show() {
  const { connected } = await chrome.storage.session.get("connected");
  state.textContent = connected ? "Connected to Jarvis." : "Not connected. Is Jarvis running?";
}

chrome.storage.local.get("port").then(({ port: p }) => { port.value = p || 47815; });
document.getElementById("go").onclick = async () => {
  await chrome.storage.local.set({ port: Number(port.value) || 47815 });
  state.textContent = "Connecting…";
  await chrome.runtime.sendMessage("reconnect");
  setTimeout(show, 800);
};
show();
