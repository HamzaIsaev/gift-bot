// Ссылка на бота — замените на реальный username
const BOT_URL = 'https://t.me/your_gift_bot';

// Демо-подарки для витрины и калькулятора. Стоимость — в звёздах Telegram (⭐), условная.
const GIFTS = [
  { id: 'bear',    name: 'Plush Bear',     emoji: '🧸', rarity: 'common',    value: 350,  c1: '#ff8a5b', c2: '#ff3d77' },
  { id: 'rose',    name: 'Eternal Rose',   emoji: '🌹', rarity: 'common',    value: 420,  c1: '#ff5d7a', c2: '#b3124a' },
  { id: 'cake',    name: 'Homemade Cake',  emoji: '🎂', rarity: 'common',    value: 300,  c1: '#ffb36b', c2: '#ff6b9a' },
  { id: 'clover',  name: 'Lucky Clover',   emoji: '🍀', rarity: 'rare',      value: 1200, c1: '#7dff9b', c2: '#00b37e' },
  { id: 'hat',     name: 'Top Hat',        emoji: '🎩', rarity: 'rare',      value: 1500, c1: '#ff7ad9', c2: '#a45bff' },
  { id: 'rocket',  name: 'Space Rocket',   emoji: '🚀', rarity: 'rare',      value: 1800, c1: '#5bd1ff', c2: '#2a6bff' },
  { id: 'ring',    name: 'Diamond Ring',   emoji: '💎', rarity: 'legendary', value: 5200, c1: '#5bd1ff', c2: '#6c5bff' },
  { id: 'star',    name: 'Golden Star',    emoji: '⭐', rarity: 'legendary', value: 4800, c1: '#ffd25b', c2: '#ff8a00' },
  { id: 'crown',   name: 'Royal Crown',    emoji: '👑', rarity: 'legendary', value: 6500, c1: '#ffe27a', c2: '#d4a017' },
];

const RARITY_LABEL = { common: 'Обычный', rare: 'Редкий', legendary: 'Легендарный' };
const byId = Object.fromEntries(GIFTS.map(g => [g.id, g]));

// Что владельцы лотов хотят получить взамен
const LOTS = [
  { gift: 'crown',  want: 'Diamond Ring + доплата' },
  { gift: 'rocket', want: 'Top Hat' },
  { gift: 'bear',   want: 'Eternal Rose' },
  { gift: 'star',   want: 'Diamond Ring' },
  { gift: 'clover', want: 'Space Rocket' },
  { gift: 'cake',   want: 'Plush Bear' },
  { gift: 'ring',   want: 'Royal Crown' },
  { gift: 'hat',    want: 'Lucky Clover + Cake' },
];

const fmt = n => n.toLocaleString('ru-RU') + ' ⭐';
const tile = g => `<div class="gift-tile" style="--c1:${g.c1};--c2:${g.c2}">${g.emoji}</div>`;

document.querySelectorAll('.bot-link').forEach(a => {
  a.href = BOT_URL;
  a.target = '_blank';
  a.rel = 'noopener';
});
document.getElementById('year').textContent = new Date().getFullYear();

// Маркет
const grid = document.getElementById('marketGrid');
function renderMarket(filter) {
  grid.innerHTML = LOTS
    .filter(l => filter === 'all' || byId[l.gift].rarity === filter)
    .map(l => {
      const g = byId[l.gift];
      return `<article class="lot">
        ${tile(g)}
        <div class="lot-top"><span class="rarity ${g.rarity}">${RARITY_LABEL[g.rarity]}</span><span class="lot-price">≈ ${fmt(g.value)}</span></div>
        <h4>${g.name}</h4>
        <div class="lot-want">Хочет: <b>${l.want}</b></div>
      </article>`;
    }).join('');
}
document.querySelectorAll('.chip').forEach(chip => {
  chip.addEventListener('click', () => {
    document.querySelectorAll('.chip').forEach(c => c.classList.toggle('active', c === chip));
    renderMarket(chip.dataset.filter);
  });
});
renderMarket('all');

// Калькулятор обмена
const giveSel = document.getElementById('giveSel');
const getSel = document.getElementById('getSel');
const options = GIFTS.map(g => `<option value="${g.id}">${g.emoji} ${g.name}</option>`).join('');
giveSel.innerHTML = options;
getSel.innerHTML = options;
giveSel.value = 'rocket';
getSel.value = 'hat';

function updateTrade() {
  const give = byId[giveSel.value];
  const get = byId[getSel.value];
  document.getElementById('givePreview').innerHTML = `${tile(give)}<span>${give.name}</span><span class="val">≈ ${fmt(give.value)}</span>`;
  document.getElementById('getPreview').innerHTML = `${tile(get)}<span>${get.name}</span><span class="val">≈ ${fmt(get.value)}</span>`;

  // diff > 0 — получаешь больше, чем отдаёшь
  const diff = (get.value - give.value) / Math.max(give.value, get.value);
  const pos = 50 + Math.max(-1, Math.min(1, diff)) * 45;
  document.getElementById('meterFill').style.left = pos + '%';

  const pct = Math.round(Math.abs(diff) * 100);
  const verdict = document.getElementById('verdict');
  if (give.id === get.id) {
    verdict.textContent = 'Это один и тот же подарок 🙂';
  } else if (pct <= 10) {
    verdict.textContent = '✅ Честный обмен — стоимость почти одинаковая.';
  } else if (diff > 0) {
    verdict.textContent = `🔥 Выгодно для тебя: получаешь на ~${pct}% больше. Второй стороне может понадобиться доплата.`;
  } else {
    verdict.textContent = `⚠️ Ты отдаёшь на ~${pct}% больше. Попроси доплату или выбери другой подарок.`;
  }
}
giveSel.addEventListener('change', updateTrade);
getSel.addEventListener('change', updateTrade);
updateTrade();
