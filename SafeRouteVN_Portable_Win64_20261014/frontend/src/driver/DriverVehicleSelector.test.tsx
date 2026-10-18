import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { App } from "../app/App";
import { MockDispatchApi } from "../services/api/MockDispatchApi";

describe("driver demo vehicle context", () => {
  it("switches to V2 in Settings without mutating DecisionState", async () => {
    const api = new MockDispatchApi({ storage: { getItem: () => null, setItem: () => {}, removeItem: () => {} } });
    const before = await api.getSnapshot();
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/driver"]}><App api={api} /></MemoryRouter>);
    await screen.findByText(/Driver App/i);
    await user.click(screen.getByRole("button", { name: /settings/i }));
    await user.selectOptions(screen.getByLabelText("Tài xế đang xem"), "V2");
    expect(screen.getByRole("heading", { name: "Trần Thị B" })).toBeInTheDocument();
    expect((await api.getSnapshot()).decisionState).toEqual(before.decisionState);
    window.history.replaceState(null,'','/');
  });
});
