// Admin music page: check the track size before uploading and suggest a display name.
const form = document.getElementById("track-upload");
if (form) {
  const file = form.querySelector("#track-file");
  const name = form.querySelector("#track-name");
  const error = form.querySelector("#track-error");
  const limit = Number(form.dataset.maxBytes);
  const showError = (message) => {
    error.textContent = message;
    error.hidden = !message;
  };
  file.addEventListener("change", () => {
    showError("");
    const chosen = file.files[0];
    if (!chosen) return;
    if (limit && chosen.size > limit) {
      showError(`This file is ${(chosen.size / 1048576).toFixed(1)} MB; the limit is ${Math.round(limit / 1048576)} MB.`);
      return;
    }
    if (!name.value.trim()) name.value = chosen.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim().slice(0, 200);
  });
  form.addEventListener("submit", (event) => {
    const chosen = file.files[0];
    if (chosen && limit && chosen.size > limit) {
      event.preventDefault();
      event.stopImmediatePropagation();
      file.focus();
    }
  }, true);
}

// Bonus type: show the fields of the chosen type (all stay visible without this script).
const modes = [...document.querySelectorAll('input[name="music_bonus_mode"]')];
const bonusFields = { multiplier: ["music_credit_multiplier"], fixed: ["music_bonus_fixed_tokens", "music_bonus_fixed_weekly_tokens"] };
function syncBonus() {
  const mode = modes.find((input) => input.checked)?.value;
  if (!mode) return;
  for (const [key, ids] of Object.entries(bonusFields)) {
    for (const id of ids) {
      const input = document.getElementById(id);
      const field = input?.closest(".field");
      // A field with an invalid value stays visible, so the browser can point at it when saving.
      if (field) field.hidden = key !== mode && input.checkValidity();
    }
  }
}
for (const input of modes) input.addEventListener("change", syncBonus);
syncBonus();
