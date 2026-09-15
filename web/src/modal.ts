// Reference counting keeps background scrolling locked across nested dialogs.
let locks = 0;
let restore = () => {};
export function lockPage() {
  if (locks === 0) {
    const body = document.body;
    const html = document.documentElement;
    const x = window.scrollX,
      y = window.scrollY;
    const previous = {
      position: body.style.position,
      top: body.style.top,
      left: body.style.left,
      width: body.style.width,
      overflow: body.style.overflow,
    };
    const overflow = html.style.overflow;
    Object.assign(body.style, {
      position: "fixed",
      top: `${-y}px`,
      left: `${-x}px`,
      width: "100%",
      overflow: "hidden",
    });
    html.style.overflow = "hidden";
    restore = () => {
      Object.assign(body.style, previous);
      html.style.overflow = overflow;
      window.scrollTo(x, y);
    };
  }
  locks += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    locks -= 1;
    if (locks === 0) restore();
  };
}
