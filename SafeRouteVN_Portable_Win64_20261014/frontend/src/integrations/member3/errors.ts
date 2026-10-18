export class Member3Error extends Error {
  constructor(public readonly code: string, message: string, public readonly status = 0,
    public readonly requestId: string | null = null, public readonly diagnostics: unknown[] = []) {
    super(message);
    this.name = "Member3Error";
  }
}
