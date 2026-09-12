import { cleanup } from "@testing-library/react";
import { afterEach } from "vite-plus/test";
afterEach(() => cleanup());
HTMLDialogElement.prototype.showModal = function () {
  this.setAttribute("open", "");
};
HTMLDialogElement.prototype.close = function () {
  this.removeAttribute("open");
};
