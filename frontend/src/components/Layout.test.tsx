import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { useChat } from "./ChatContext";
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
    expect(within(main).queryByRole("link", { name: "Tasks" })).toBeNull(); // tasks live in each project
    expect(within(main).getByRole("link", { name: "Projects" })).toBeInTheDocument();
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


function Asker() {
  const chat = useChat();
  return <button onClick={() => chat.open({ prompt: "Why is demo paused?" })}>Ask about this</button>;
}

function inProject(path = "/p/demo", routes: Record<string, unknown> = {}) {
  const api = mockApi({
    "GET /api/inbox": [],
    "GET /api/todos": { todos: [] },
    "GET /api/chat/repos": { repos: [{ id: "studio", label: "Agent Studio (this app)" }], cap_usd: 1 },
    "GET /api/chats?project=demo": { chats: [] },
    ...routes,
  });
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<div>the list</div>} />
          <Route path="p/:slug" element={<Asker />} />
          <Route path="p/:slug/team" element={<div>the team</div>} />
          <Route path="p/:other/more" element={<div>another</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
  return api;
}

describe("the chat sidebar in the layout", () => {
  it("has no Chat entry in the main bar and no button outside a project", async () => {
    inProject("/");
    const main = await screen.findByRole("navigation", { name: "Main" });

    expect(within(main).queryByRole("link", { name: "Chat" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Chat" })).toBeNull();
  });

  it("opens and closes from the Chat button inside a project", async () => {
    inProject();
    const button = await screen.findByRole("button", { name: "Chat" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("complementary", { name: "Chat" })).toBeNull();

    await userEvent.click(button);
    expect(await screen.findByRole("complementary", { name: "Chat" })).toBeInTheDocument();
    expect(button).toHaveAttribute("aria-expanded", "true");

    await userEvent.click(button);
    expect(screen.queryByRole("complementary", { name: "Chat" })).toBeNull();
  });

  it("stays open while you move around the project", async () => {
    inProject();
    await userEvent.click(await screen.findByRole("button", { name: "Chat" }));
    await screen.findByRole("complementary", { name: "Chat" });

    expect(screen.getByRole("complementary", { name: "Chat" })).toBeInTheDocument();
  });

  it("a page can open it with a prefilled question", async () => {
    inProject();

    await userEvent.click(await screen.findByRole("button", { name: "Ask about this" }));

    expect(await screen.findByRole("complementary", { name: "Chat" })).toBeInTheDocument();
    expect(await screen.findByLabelText("Message")).toHaveValue("Why is demo paused?");
  });
});
