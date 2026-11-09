// Talking to the server. Reads are plain GETs; every change carries the session token in a
// header and a JSON body, which is what the server requires to know it came from this app.

export class ApiError extends Error {
  readonly status: number;
  readonly problems: string[];

  constructor(status: number, message: string, problems: string[] = []) {
    super(message);
    this.status = status;
    this.problems = problems;
  }
}

let token: Promise<string> | null = null;

function sessionToken(): Promise<string> {
  token ??= fetch("/api/session")
    .then((r) => r.json())
    .then((j: { csrf: string }) => j.csrf);
  return token;
}

async function parse<T>(response: Response): Promise<T> {
  const body: unknown = await response.json().catch(() => ({}));
  if (!response.ok) {
    const data = body as { error?: string; problems?: string[] };
    throw new ApiError(
      response.status,
      data.error ?? data.problems?.join(" ") ?? `Request failed (${response.status})`,
      data.problems ?? [],
    );
  }
  return body as T;
}

export async function get<T>(url: string): Promise<T> {
  return parse<T>(await fetch(url));
}

export async function post<T>(url: string, body: unknown = {}): Promise<T> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json", "x-csrf-token": await sessionToken() },
    body: JSON.stringify(body),
  });
  return parse<T>(response);
}

/**
 * A change whose answer arrives as Server-Sent Events; `onEvent` is called for each one as it lands.
 * Aborting the `signal` closes the stream, which stops the answer on the server.
 */
export async function postStream<T>(
  url: string,
  body: unknown,
  onEvent: (event: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json", "x-csrf-token": await sessionToken() },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok || response.body === null) {
    await parse<unknown>(response);
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end = buffer.indexOf("\n\n");
    while (end !== -1) {
      const line = buffer.slice(0, end).split("\n").find((l) => l.startsWith("data: "));
      buffer = buffer.slice(end + 2);
      if (line) onEvent(JSON.parse(line.slice(6)) as T);
      end = buffer.indexOf("\n\n");
    }
  }
}

export function resetSessionForTests(): void {
  token = null;
}
