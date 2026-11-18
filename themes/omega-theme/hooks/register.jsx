import { theme } from "./palette.js"
import { SCENE } from "./scene.js"

const SCENE_NAME = "omega-orbit"

export function register(on) {
  on("session.start", async ($, e, next) => {
    // One call names the whole theme: the colors and the scene behind the conversation.
    // A window that attaches later still draws the theme, so a session without one needs no notice.
    await $.theme.set({
      colors: theme,
      backdrop: { scene: SCENE_NAME, glass: { blur: 10, tint: "#17212a", opacity: 0.55 } },
    })

    return next(e)
  })

  on("ui.render", { component: "Backdrop" }, ($, e, next) => {
    if (e.props.scene !== SCENE_NAME || (e.surface !== "desktop" && e.surface !== "mobile")) {
      return next(e)
    }

    const { Svg } = $.ui.resolve(e)

    return <Svg source={SCENE} alt="A night sky of gold sparkles, with a head in headphones inside an orbit ring" isInteractive />
  })
}
