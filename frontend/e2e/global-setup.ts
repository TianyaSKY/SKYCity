async function requireUrl(name: string, url: string): Promise<void> {
    try {
        const response = await fetch(url);
        if (!response.ok) {
            throw new Error(`${response.status} ${response.statusText}`);
        }
    } catch (error) {
        throw new Error(
            `${name} is not reachable at ${url}. Start the backend and frontend dev servers before npm run test:e2e. Cause: ${String(error)}`,
        );
    }
}

export default async function globalSetup(): Promise<void> {
    await requireUrl('Backend', `${process.env.E2E_API_URL ?? 'http://127.0.0.1:8000'}/health`);
    await requireUrl('Frontend', process.env.E2E_BASE_URL ?? 'http://127.0.0.1:5173');
}
