import { useEffect, useState } from "react";
import { create } from "zustand";
import { Drawer, DrawerPresence, Icon } from "./components";
import s from "./ui.module.scss";
import "./theme.scss";

export type Mode = "system" | "light" | "dark";
export type Palette = "green" | "blue" | "rose" | "amber";
const key = "codebuddy.appearance";
const modes: Mode[] = ["system", "light", "dark"];
const palettes: Palette[] = ["green", "blue", "rose", "amber"];
export function parseAppearance(raw: string | null): { mode: Mode; palette: Palette } {
  try {
    const value = JSON.parse(raw ?? "null") as { mode?: Mode; palette?: Palette } | null;
    return {
      mode: modes.includes(value?.mode as Mode) ? value!.mode! : "system",
      palette: palettes.includes(value?.palette as Palette) ? value!.palette! : "green",
    };
  } catch {
    return { mode: "system", palette: "green" };
  }
}
function savedAppearance() {
  try {
    return parseAppearance(localStorage.getItem(key));
  } catch {
    return parseAppearance(null);
  }
}
export const useAppearance = create(() => ({ ...savedAppearance(), dark: false, persisted: true }));
export function initializeAppearance() {
  const media = window.matchMedia?.("(prefers-color-scheme: dark)");
  const apply = () => {
    const { mode, palette } = useAppearance.getState();
    const dark = mode === "dark" || (mode === "system" && !!media?.matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.documentElement.dataset.palette = palette;
    if (dark !== useAppearance.getState().dark) useAppearance.setState({ dark });
  };
  apply();
  media?.addEventListener("change", apply);
  const unsubscribe = useAppearance.subscribe(apply);
  const storage = (event: StorageEvent) => {
    if (event.key === key || event.key === null)
      useAppearance.setState({ ...savedAppearance(), persisted: true });
  };
  window.addEventListener("storage", storage);
  return () => {
    media?.removeEventListener("change", apply);
    unsubscribe();
    window.removeEventListener("storage", storage);
  };
}
export function chooseAppearance(value: Partial<{ mode: Mode; palette: Palette }>) {
  const next = {
    mode: useAppearance.getState().mode,
    palette: useAppearance.getState().palette,
    ...value,
  };
  let persisted = true;
  try {
    localStorage.setItem(key, JSON.stringify(next));
  } catch {
    persisted = false;
  }
  useAppearance.setState({ ...next, persisted });
}
export function Appearance() {
  const [open, setOpen] = useState(false);
  const { mode, palette, dark, persisted } = useAppearance();
  useEffect(initializeAppearance, []);
  return (
    <>
      <button
        className={s.appearanceButton}
        aria-label="外观设置"
        title="外观设置"
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen(true)}
      >
        <Icon name="appearance" />
      </button>
      <DrawerPresence>
        {open && (
          <Drawer title="外观设置" className={s.appearanceDrawer} onClose={() => setOpen(false)}>
            <fieldset className={s.appearanceChoices}>
              <legend>显示模式</legend>
              {(
                [
                  ["system", "跟随系统", "system"],
                  ["light", "浅色", "sun"],
                  ["dark", "深色", "moon"],
                ] as const
              ).map(([value, label, icon]) => (
                <label key={value}>
                  <input
                    type="radio"
                    name="appearance-mode"
                    value={value}
                    checked={mode === value}
                    onChange={() => chooseAppearance({ mode: value })}
                  />
                  <Icon name={icon} />
                  <span>{label}</span>
                </label>
              ))}
            </fieldset>
            <fieldset className={s.paletteChoices} disabled={dark}>
              <legend>浅色配色</legend>
              {(
                [
                  ["green", "青叶", "#6c9852"],
                  ["blue", "雾蓝", "#527cab"],
                  ["rose", "蔷薇", "#aa617b"],
                  ["amber", "暖砂", "#a7804b"],
                ] as const
              ).map(([value, label, color]) => (
                <label key={value}>
                  <input
                    type="radio"
                    name="appearance-palette"
                    value={value}
                    checked={palette === value}
                    onChange={() => chooseAppearance({ palette: value })}
                  />
                  <span style={{ background: color }} />
                  <span>{label}</span>
                </label>
              ))}
            </fieldset>
            <p className={s.note}>
              {dark
                ? "深色使用固定配色；浅色偏好会保留，切回浅色后生效。"
                : "配色只应用于浅色界面，不改变状态颜色。"}
            </p>
            {!persisted && <p role="status">浏览器无法保存偏好，本次会话内仍然生效。</p>}
          </Drawer>
        )}
      </DrawerPresence>
    </>
  );
}
