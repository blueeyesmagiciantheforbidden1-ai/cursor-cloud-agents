export class AdapterError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AdapterError";
  }
}

export class HubError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "HubError";
    this.status = status;
  }
}
