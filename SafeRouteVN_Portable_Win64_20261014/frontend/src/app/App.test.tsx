import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { App } from "./App";
import { MockDispatchApi } from "../services/api/MockDispatchApi";

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App api={new MockDispatchApi({storage:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}}})} />
    </MemoryRouter>,
  );
}

describe("App routes", () => {
  it("shows the dispatcher workspace at /admin", async () => {
    renderAt("/admin");

    expect(await screen.findByRole("heading", { name: /điều phối/i })).toBeInTheDocument();
  });

  it("shows the driver workspace at /driver", async () => {
    renderAt("/driver");

    expect(await screen.findByRole("heading", { name: /tài xế/i })).toBeInTheDocument();
  });

  it("falls back to the dispatcher workspace", async () => {
    renderAt("/khong-ton-tai");

    expect(await screen.findByRole("heading", { name: /điều phối/i })).toBeInTheDocument();
  });
});
