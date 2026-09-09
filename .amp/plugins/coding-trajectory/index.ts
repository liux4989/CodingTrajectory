import type { PluginAPI, PluginThread, ThreadMessage } from '@ampcode/plugin'
import { spawn } from 'node:child_process'
import { constants } from 'node:fs'
import { access, appendFile, mkdir, readFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { join } from 'node:path'

export const description =
	'Captures Amp threads locally and triggers privacy-safe Chronicle publication.'

const SCHEMA_VERSION = 1
const PAGE_SIZE = 20
const PUBLISH_DEBOUNCE_MS = 2_000

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

function publisherCommand(): string | null {
	if (process.env.CT_AMP_AUTO_PUBLISH === '0') return null
	const configured = process.env.CT_AMP_PUBLISH_COMMAND
	if (!configured) {
		return join(homedir(), '.coding-trajectory', 'bin', 'run-chronicle-collector')
	}
	return configured.startsWith('~/')
		? join(homedir(), configured.slice(2))
		: configured
}

function recordKey(record: Record<string, unknown>): string | null {
	if (record.type === 'thread') return 'thread'
	if (record.type === 'observation') {
		const event = record.event
		const threadID = record.thread_id
		if (typeof event !== 'string' || typeof threadID !== 'string') return null
		if (
			(event === 'agent.start' || event === 'agent.end') &&
			(typeof record.message_id === 'string' || typeof record.message_id === 'number')
		) {
			return `observation:${event}:${threadID}:${record.message_id}`
		}
		if (
			(event === 'tool.call' || event === 'tool.result') &&
			typeof record.tool_use_id === 'string'
		) {
			return `observation:${event}:${threadID}:${record.tool_use_id}`
		}
		return null
	}
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
			if (key) {
				state.set(
					key,
					record.type === 'observation'
						? JSON.stringify(key)
						: JSON.stringify(record.payload ?? record.message),
				)
			}
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
	let publishTimer: ReturnType<typeof setTimeout> | undefined
	let publishRunning = false
	let publishRequested = false
	let disposed = false
	let unavailableLogged = false

	function requestPublication(): void {
		const command = publisherCommand()
		const workspaceRoot = amp.system.workspaceRoot
		if (!command || !workspaceRoot) return
		publishRequested = true
		if (publishRunning || publishTimer) return
		publishTimer = setTimeout(() => {
			publishTimer = undefined
			void launchPublisher(command, amp.helpers.filePathFromURI(workspaceRoot))
		}, PUBLISH_DEBOUNCE_MS)
	}

	async function launchPublisher(command: string, cwd: string): Promise<void> {
		try {
			await access(command, constants.X_OK)
		} catch (error) {
			if (!unavailableLogged && process.env.CT_AMP_PUBLISH_COMMAND) {
				unavailableLogged = true
				amp.logger.log('CodingTrajectory publisher is not executable:', error)
			}
			return
		}

		publishRequested = false
		publishRunning = true
		const child = spawn(command, [], {
			cwd,
			detached: true,
			stdio: 'ignore',
		})
		child.once('error', (error) => {
			publishRunning = false
			if (!disposed) {
				amp.logger.log('CodingTrajectory publisher failed to start:', error)
				if (publishRequested) requestPublication()
			}
		})
		child.once('exit', (code) => {
			publishRunning = false
			if (!disposed && code !== 0) {
				amp.logger.log(`CodingTrajectory publisher exited with status ${code}.`)
			}
			if (!disposed && publishRequested) requestPublication()
		})
		child.unref()
	}

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

	async function stateForThread(threadID: string): Promise<{
		path: string
		state: StoredState
	}> {
		const path = sourcePath(threadID)
		await mkdir(logRoot(), { recursive: true, mode: 0o700 })
		let state = states.get(path)
		if (!state) {
			state = await loadState(path)
			states.set(path, state)
		}
		return { path, state }
	}

	function queue(write: () => Promise<void>, failure: string): Promise<void> {
		const run = captures.then(write)
		captures = run.catch((error) => {
			amp.logger.log(failure, error)
		})
		return captures
	}

	function observe(
		threadID: string,
		event: string,
		observedAt: string,
		fields: Record<string, unknown> = {},
	): Promise<void> {
		return queue(async () => {
			const { path, state } = await stateForThread(threadID)
			const record = {
				schema_version: SCHEMA_VERSION,
				type: 'observation',
				captured_at: observedAt,
				observed_at: observedAt,
				thread_id: threadID,
				event,
				...fields,
			}
			const key = recordKey(record)
			if (key) {
				await appendChanged(path, state, key, key, record)
			} else {
				await appendFile(path, `${JSON.stringify(record)}\n`, {
					encoding: 'utf8',
					mode: 0o600,
				})
			}
		}, `CodingTrajectory observation failed for ${threadID}:`)
	}

	function capture(
		thread: PluginThread,
		trigger: string,
		capturedAt: string,
	): Promise<void> {
		return queue(async () => {
			const { path, state } = await stateForThread(thread.id)
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
		}, `CodingTrajectory capture failed for ${thread.id}:`)
	}

	amp.on('session.start', (_event, ctx) => {
		const observedAt = new Date().toISOString()
		void observe(ctx.thread.id, 'session.start', observedAt)
		return capture(ctx.thread, 'session.start', observedAt)
	})
	amp.on('agent.start', async (event, ctx) => {
		const observedAt = new Date().toISOString()
		await observe(ctx.thread.id, 'agent.start', observedAt, {
			message_id: event.id,
		})
		await capture(ctx.thread, 'agent.start', observedAt)
		return {}
	})
	amp.on('agent.end', (event, ctx) => {
		const observedAt = new Date().toISOString()
		void observe(ctx.thread.id, 'agent.end', observedAt, {
			message_id: event.id,
			status: event.status,
		})
		return capture(ctx.thread, 'agent.end', observedAt).then(requestPublication)
	})
	amp.on('tool.call', async (event, ctx) => {
		const observedAt = new Date().toISOString()
		await observe(ctx.thread.id, 'tool.call', observedAt, {
			tool_use_id: event.toolUseID,
			tool_name: event.tool,
			...(event.input !== undefined ? { input: event.input } : {}),
		})
		return { action: 'allow' }
	})
	amp.on('tool.result', (event, ctx) => {
		const observedAt = new Date().toISOString()
		return observe(ctx.thread.id, 'tool.result', observedAt, {
			tool_use_id: event.toolUseID,
			status: event.status,
			tool_name: event.tool,
			...(event.input !== undefined ? { input: event.input } : {}),
			...(event.output !== undefined ? { output: event.output } : {}),
			...(event.error !== undefined ? { error: event.error } : {}),
		})
	})

	const active = amp.activeThread.current
	if (active) {
		void capture(
			amp.threads.get(active.id),
			'plugin.load',
			new Date().toISOString(),
		).then(requestPublication)
	}
	const activeSubscription = amp.activeThread.subscribe((current) => {
		if (current) {
			void capture(
				amp.threads.get(current.id),
				'thread.active',
				new Date().toISOString(),
			)
		}
	})
	amp.onDispose(() => {
		disposed = true
		if (publishTimer) clearTimeout(publishTimer)
		activeSubscription.unsubscribe()
	})
}
