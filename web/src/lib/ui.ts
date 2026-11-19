/* App-wide UI services (dialogs, context menus) provided by the workbench. */
import { createContext, useContext } from "react";
import type { DialogSpec, MenuItem } from "../components/overlays";

export interface UiApi {
  dialog: (spec: DialogSpec) => void;
  menu: (x: number, y: number, items: MenuItem[]) => void;
  /** Ask before closing a tab with unsaved changes; resolves once handled. */
  closeTab: (id: string) => void;
}
export const UiContext = createContext<UiApi>({ dialog: () => {}, menu: () => {}, closeTab: () => {} });
export const useUi = () => useContext(UiContext);
