import { LOGO, LOGO_HEIGHT, LOGO_WIDTH } from "./logo.js"

// The Omega scene: a night sky with slow gold sparkles, and the logo (a head in headphones inside an
// orbit ring) floating in the upper right, away from the words and the message box.
// One SVG, animated by its own CSS: it runs no script and loads nothing.

const SPARKLE = 'M0-10Q1.5-1.5 10 0Q1.5 1.5 0 10Q-1.5 1.5-10 0Q-1.5-1.5 0-10Z'
const RING = 'M-14 543A545 172 0 1 1 1076 543A545 172 0 1 1 -14 543'

/** A small seeded generator, so the sky is the same at every load. */
function random(seed) {
  let state = seed

  return () => {
    state = (state * 1664525 + 1013904223) % 4294967296

    return state / 4294967296
  }
}

function sky() {
  const next = random(106)
  const stars = []

  for (let i = 0; i < 90; i++) {
    const x = (next() * 1600).toFixed(0)
    const y = (next() * 1000).toFixed(0)
    const r = (0.6 + next() * 1.3).toFixed(1)
    const delay = (next() * -9).toFixed(1)
    const fill = next() < 0.3 ? '#fbbc1d' : '#f4f6f8'

    stars.push(`<circle class="dot" cx="${x}" cy="${y}" r="${r}" fill="${fill}" style="animation-delay:${delay}s"/>`)
  }

  for (let i = 0; i < 14; i++) {
    const x = (next() * 1600).toFixed(0)
    const y = (next() * 1000).toFixed(0)
    const scale = (0.7 + next() * 1.4).toFixed(2)
    const delay = (next() * -12).toFixed(1)
    const fill = next() < 0.5 ? '#ff7e07' : '#fbbc1d'

    stars.push(
      `<g transform="translate(${x} ${y}) scale(${scale})"><path class="spark" d="${SPARKLE}" fill="${fill}" style="animation-delay:${delay}s"/></g>`,
    )
  }

  return stars.join('')
}

const STYLE = `
.dot{animation:twinkle 9s ease-in-out infinite}
.spark{animation:flare 12s ease-in-out infinite;transform-box:fill-box;transform-origin:center}
.logo{animation:float 11s ease-in-out infinite}
.glow{animation:pulse 5s ease-in-out infinite}
.comet{offset-path:path('${RING}');animation:orbit 16s linear infinite}
.shoot{animation:shoot 14s ease-in infinite}
@keyframes twinkle{0%,100%{opacity:.25}50%{opacity:.9}}
@keyframes flare{0%,100%{opacity:.15;transform:scale(.6) rotate(0deg)}50%{opacity:.95;transform:scale(1.15) rotate(45deg)}}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-14px)}}
@keyframes pulse{0%,100%{opacity:.35}50%{opacity:.9}}
@keyframes orbit{0%{offset-distance:0%;opacity:.25}45%{opacity:.25}55%{opacity:1}95%{opacity:1}100%{offset-distance:100%;opacity:.25}}
@keyframes shoot{0%,86%{opacity:0;transform:translate(0,0)}88%{opacity:.9}100%{opacity:0;transform:translate(-520px,260px)}}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
`

const DEFS = `
<linearGradient id="night" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#10171e"/><stop offset="1" stop-color="#17212a"/></linearGradient>
<radialGradient id="halo" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="#ff7e07" stop-opacity=".30"/><stop offset=".55" stop-color="#ff6905" stop-opacity=".08"/><stop offset="1" stop-color="#ff6905" stop-opacity="0"/></radialGradient>
<linearGradient id="tail" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fbbc1d"/><stop offset="1" stop-color="#fbbc1d" stop-opacity="0"/></linearGradient>
`

// What moves around the traced logo, in its own box: a halo behind it, a glow on the earcup light, and a comet
// on an ellipse laid over the ring (it dims on the far half, behind the head).
const BEHIND = `<ellipse cx="533" cy="420" rx="545" ry="425" fill="url(#halo)"/>`
const OVER = `
<ellipse class="glow" cx="721" cy="553" rx="62" ry="82" fill="url(#halo)"/>
<g transform="rotate(-24.7 531 543)"><circle class="comet" r="9" fill="#f4f6f8" stroke="#fbbc1d" stroke-width="4"/></g>
`

export const SCENE =
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 1000" width="100%" height="100%" preserveAspectRatio="xMidYMid slice">` +
  `<style>${STYLE}</style><defs>${DEFS}</defs>` +
  `<rect width="1600" height="1000" fill="url(#night)"/>` +
  sky() +
  `<g class="shoot"><rect x="1240" y="150" width="150" height="3" rx="1.5" fill="url(#tail)" transform="rotate(-27 1240 150)"/></g>` +
  `<g class="shoot" style="animation-delay:-7s"><rect x="620" y="90" width="120" height="2.5" rx="1.2" fill="url(#tail)" transform="rotate(-27 620 90)"/></g>` +
  `<svg x="930" y="40" width="560" height="442" viewBox="0 0 ${LOGO_WIDTH} ${LOGO_HEIGHT}" overflow="visible"><g class="logo">${BEHIND}${LOGO}${OVER}</g></svg>` +
  `</svg>`
