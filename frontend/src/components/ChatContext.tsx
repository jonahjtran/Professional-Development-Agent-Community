import { createContext, useContext } from "react";

/** What a page can ask the chat sidebar to start with. */
export interface ChatRequest {
  prompt?: string;
  question?: string; // an open question of the project, given to the assistant as context
}

export interface ChatControl {
  isOpen: boolean;
  request: ChatRequest | null;
  open: (request?: ChatRequest) => void;
  close: () => void;
  toggle: () => void;
}

export const ChatContext = createContext<ChatControl>({
  isOpen: false,
  request: null,
  open: () => {},
  close: () => {},
  toggle: () => {},
});

export const useChat = () => useContext(ChatContext);
