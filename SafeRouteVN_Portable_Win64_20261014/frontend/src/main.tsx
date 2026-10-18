import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./app/App";
import { DriverAccess } from './app/DriverAccess';
import "./styles/global.css";

const root = document.getElementById("root");

if (!root) {
  throw new Error("Không tìm thấy phần tử #root.");
}

createRoot(root).render(
  <StrictMode>
    <BrowserRouter>
      {import.meta.env.VITE_REMOTE_DRIVER === '1' ? <DriverAccess /> : <App />}
    </BrowserRouter>
  </StrictMode>,
);
