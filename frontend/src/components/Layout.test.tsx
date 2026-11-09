import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { Layout } from "./Layout";
import { mockApi } from "../test/mockApi";

function show() {
  mockApi({ "GET /api/inbox": [], "GET /api/todos": { todos: [] } });
  render(<MemoryRouter><Layout /></MemoryRouter>);
}

describe("the profile menu", () => {
  it("takes Models and Rules out of the main bar", async () => {
    show();
    const main = await screen.findByRole("navigation", { name: "Main" });

    expect(within(main).queryByRole("link", { name: "Models" })).toBeNull();
    expect(within(main).queryByRole("link", { name: "Rules" })).toBeNull();
    expect(within(main).getByRole("link", { name: "Tasks" })).toBeInTheDocument();
  });

  it("opens from the profile button and lists Models and Rules, and nothing that does not exist", async () => {
    show();
    const button = await screen.findByRole("button", { name: "Profile and settings" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("menu")).toBeNull();

    await userEvent.click(button);

    const menu = screen.getByRole("menu");
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(within(menu).getAllByRole("menuitem").map((i) => [i.textContent, i.getAttribute("href")])).toEqual([
      ["Models", "/models"],
      ["Rules", "/rules"],
    ]);
  });

  it("closes on Escape, on a click elsewhere and after choosing an item", async () => {
    show();
    const button = await screen.findByRole("button", { name: "Profile and settings" });

    await userEvent.click(button);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();

    await userEvent.click(button);
    await userEvent.click(document.body);
    expect(screen.queryByRole("menu")).toBeNull();

    await userEvent.click(button);
    await userEvent.click(screen.getByRole("menuitem", { name: "Models" }));
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("marks the profile button active on the Models and Rules pages", async () => {
    mockApi({ "GET /api/inbox": [], "GET /api/todos": { todos: [] } });
    render(<MemoryRouter initialEntries={["/rules"]}><Layout /></MemoryRouter>);

    expect(await screen.findByRole("button", { name: "Profile and settings" })).toHaveClass("active");
  });
});
