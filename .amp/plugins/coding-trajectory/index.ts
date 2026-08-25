import type { PluginAPI, PluginThread, ThreadMessage } from '@ampcode/plugin'
import { appendFile, mkdir, readFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { join } from 'node:path'

export const description =
	'Captures Amp thread transcripts as local append-only JSONL for CodingTrajectory.'

const SCHEMA_VERSION = 1
const PAGE_SIZE = 20

type StoredState = Map<string, string>

function logRoot(): string {
	return (
		process.env.CT_AMP_LOG_DIR ??
		join(homedir(), '.coding-trajectory', 'amp', 'sessions')
	)
}

function sourcePath(threadID: string): string {
	const safeID = threadID.replace(/[^a-zA-Z0-9-]/g, '_')
	return join(logRoot(), `${safeID}.jsonl`)
}

function recordKey(record: Record<string, unknown>): string | null {
	if (record.type === 'thread') return 'thread'
	if (record.type !== 'message') return null
	const message = record.message
	if (!message || typeof message !== 'object' || !('id' in message)) return null
	return `message:${String(message.id)}`
}

async function loadState(path: string): Promise<StoredState> {
	const state: StoredState = new Map()
	let contents: string
	try {
		contents = await readFile(path, 'utf8')
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === 'ENOENT') return state
		throw error
	}

	for (const line of contents.split('\n')) {
		if (!line) continue
		try {
			const record = JSON.parse(line) as Record<string, unknown>
			const key = recordKey(record)
			if (key) state.set(key, JSON.stringify(record.payload ?? record.message))
		} catch {
			// Ignore a partial line left by an interrupted write. The next capture
			// appends the current revision of every missing message.
		}
	}
	if (contents && !contents.endsWith('\n')) {
		await appendFile(path, '\n', { encoding: 'utf8', mode: 0o600 })
	}
	return state
}

async function allMessages(thread: PluginThread): Promise<ThreadMessage[]> {
	const messages: ThreadMessage[] = []
	for (let offset = 0; ; offset += PAGE_SIZE) {
		const page = await thread.messages({
			full: true,
			from: 'start',
			offset,
			limit: PAGE_SIZE,
		})
		messages.push(...page)
		if (page.length < PAGE_SIZE) return messages
	}
}

async function optional<T>(read: () => Promise<T>): Promise<T | null> {
	try {
		return await read()
	} catch {
		return null
	}
}

export default function codingTrajectoryCollector(amp: PluginAPI) {
	const states = new Map<string, StoredState>()
	let captures = Promise.resolve()

	async function appendChanged(
		path: string,
		state: StoredState,
		key: string,
		payload: unknown,
		record: Record<string, unknown>,
	): Promise<void> {
		const serializedPayload = JSON.stringify(payload)
		if (state.get(key) === serializedPayload) return
		await appendFile(path, `${JSON.stringify(record)}\n`, {
			encoding: 'utf8',
			mode: 0o600,
		})
		state.set(key, serializedPayload)
	}

	function capture(thread: PluginThread, trigger: string): Promise<void> {
		const run = captures.then(async () => {
			const path = sourcePath(thread.id)
			await mkdir(logRoot(), { recursive: true, mode: 0o700 })
			let state = states.get(path)
			if (!state) {
				state = await loadState(path)
				states.set(path, state)
			}

			const capturedAt = new Date().toISOString()
			const title = await optional(() => thread.title.get())
			const parentThreadID = await optional(() => thread.parentThreadID())
			const threadPayload = {
				id: thread.id,
				title,
				parent_thread_id: parentThreadID,
				workspace_root: amp.system.workspaceRoot?.toString() ?? null,
				executor: amp.system.executor.kind,
			}
			await appendChanged(path, state, 'thread', threadPayload, {
				schema_version: SCHEMA_VERSION,
				type: 'thread',
				captured_at: capturedAt,
				trigger,
				payload: threadPayload,
			})

			for (const message of await allMessages(thread)) {
				await appendChanged(path, state, `message:${String(message.id)}`, message, {
					schema_version: SCHEMA_VERSION,
					type: 'message',
					captured_at: capturedAt,
					trigger,
					thread_id: thread.id,
					message,
				})
			}
		})

		captures = run.catch((error) => {
			amp.logger.log(
				`CodingTrajectory capture failed for ${thread.id}:`,
				error,
			)
		})
		return captures
	}

	amp.on('session.start', (_event, ctx) => capture(ctx.thread, 'session.start'))
	amp.on('agent.start', (_event, ctx) => capture(ctx.thread, 'agent.start'))
	amp.on('agent.end', (_event, ctx) => capture(ctx.thread, 'agent.end'))

	const active = amp.activeThread.current
	if (active) void capture(amp.threads.get(active.id), 'plugin.load')
	const activeSubscription = amp.activeThread.subscribe((current) => {
		if (current) void capture(amp.threads.get(current.id), 'thread.active')
	})
	amp.onDispose(() => activeSubscription.unsubscribe())
}
