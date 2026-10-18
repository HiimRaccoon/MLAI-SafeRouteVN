import { Navigate, Route, Routes } from "react-router-dom";
import { AdminPage } from "../admin/AdminPage";
import { DriverPage } from "../driver/DriverPage";
import type { DispatchApi } from "../services/api/DispatchApi";
import { DispatchProvider } from "./DispatchContext";

export function App({ api }: { api?: DispatchApi }) {
  return (
    <DispatchProvider api={api}>
      <Routes>
        <Route path="/admin" element={import.meta.env.VITE_REMOTE_DRIVER === "1" ? <Navigate replace to="/driver" /> : <AdminPage />} />
        <Route path="/driver" element={<DriverPage />} />
        <Route path="*" element={<Navigate replace to={import.meta.env.VITE_REMOTE_DRIVER === "1" ? "/driver" : "/admin"} />} />
      </Routes>
    </DispatchProvider>
  );
}
