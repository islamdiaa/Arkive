<svelte:head>
	<title>Setup | Arkive</title>
</svelte:head>

<script lang="ts">
	import { api } from '$lib/api/client';
	import { setupCompleted, addToast } from '$lib/stores/app';
	import { applySession } from '$lib/stores/auth';
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';
	import CronEditor from '$lib/components/shared/CronEditor.svelte';

	let step = 1;
	let hydrated = false;
	let loading = false;
	let generatedKey = '';

	// Step 1: Encryption password
	let encryptionPassword = '';
	let confirmPassword = '';

	// Step 2: Container Discovery
	let containers: any[] = [];
	let scanning = false;
	let scanError = '';
	let scanDone = false;

	// Step 3: Storage destination (BYOS)
	type StorageProvider = 'b2' | 's3' | 'wasabi' | 'dropbox' | 'gdrive' | 'sftp' | 'local';
	let storageProvider: StorageProvider | '' = '';

	// Provider credentials
	let b2KeyId = '';
	let b2AppKey = '';
	let b2Bucket = '';

	let s3Endpoint = '';
	let s3AccessKey = '';
	let s3SecretKey = '';
	let s3Bucket = '';
	let s3Region = 'us-east-1';

	let wasabiAccessKey = '';
	let wasabiSecretKey = '';
	let wasabiBucket = '';
	let wasabiRegion = 'us-east-1';

	let dropboxToken = '';
	let gdriveToken = '';

	let sftpHost = '';
	let sftpPort = 22;
	let sftpUsername = '';
	let sftpPassword = '';
	let sftpPath = '/backups/arkive';

	let localPath = '/mnt/user/backups';

	// Step 4: Schedules
	let dbDumpSchedule = '0 6,18 * * *';
	let cloudSyncSchedule = '0 7 * * *';
	let flashSchedule = '0 6 * * *';

	// Step 5: Directories
	let directories: string[] = [];
	let newDir = '';
	let directoryScanPlatform = '';
	let setupCoverageHint = '';

	const providers: { value: StorageProvider; label: string; desc: string; icon: string }[] = [
		{ value: 'b2', label: 'Backblaze B2', desc: 'S3-compatible object storage. Great value.', icon: '🔵' },
		{ value: 's3', label: 'Amazon S3', desc: 'AWS S3 or any S3-compatible endpoint.', icon: '🟠' },
		{ value: 'wasabi', label: 'Wasabi', desc: 'Hot storage, no egress fees.', icon: '🟢' },
		{ value: 'dropbox', label: 'Dropbox', desc: 'Connect your Dropbox account via OAuth.', icon: '📦' },
		{ value: 'gdrive', label: 'Google Drive', desc: 'Use your Google Drive storage.', icon: '🔺' },
		{ value: 'sftp', label: 'SFTP Server', desc: 'Any SSH/SFTP accessible server.', icon: '🖥️' },
		{ value: 'local', label: 'Local Path', desc: 'Local or network-mounted directory.', icon: '📁' },
	];

	// Reactive storage validation — must reference variables explicitly for Svelte 5 compat reactivity
	$: storageValid = (() => {
		// Reference all provider variables so Svelte tracks them
		void storageProvider; void b2KeyId; void b2AppKey; void b2Bucket;
		void s3AccessKey; void s3SecretKey; void s3Bucket;
		void wasabiAccessKey; void wasabiSecretKey; void wasabiBucket;
		void dropboxToken; void gdriveToken;
		void sftpHost; void sftpUsername;
		void localPath;
		return isStorageValid();
	})();

	// Only save AFTER hydration is complete to avoid overwriting restored step
	$: if (hydrated && typeof sessionStorage !== 'undefined') {
		sessionStorage.setItem('arkive_setup_step', String(step));
	}

	onMount(() => {
		api.getSession().then(session => {
			if (session.setup_required) {
				return;
			}
			if (session.authenticated) {
				goto('/');
			} else {
				goto('/login');
			}
		}).catch(() => {
			// Ignore — stay on setup page
		});

		// Restore wizard step from sessionStorage
		if (typeof sessionStorage !== 'undefined') {
			const saved = sessionStorage.getItem('arkive_setup_step');
			if (saved) {
				const parsed = parseInt(saved, 10);
				if (parsed >= 1 && parsed <= 5) {
					step = parsed;
				}
			}
		}

		api.scanDirectories().then((scan: any) => {
			directoryScanPlatform = scan?.platform || '';
			if (directories.length > 0) return;
			const appdata = (scan?.directories || []).find((entry: any) => entry.path === '/mnt/user/appdata');
			if (scan?.platform === 'unraid' && appdata) {
				directories = [appdata.path];
				setupCoverageHint = 'Arkive preselected /mnt/user/appdata so Unraid app configuration is protected by default.';
			}
		}).catch(() => {
			// Non-fatal. Setup can continue without directory suggestions.
		});

		hydrated = true;
	});

	async function scanContainers() {
		scanning = true;
		scanError = '';
		try {
			const res = await fetch('/api/discover/scan', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include'
			});
			if (res.ok) {
				const data = await res.json();
				containers = data.containers || [];
			} else {
				scanError = 'Docker not available — you can add databases manually later';
			}
		} catch {
			scanError = 'Could not connect to Docker — you can skip this step';
		} finally {
			scanning = false;
			scanDone = true;
		}
	}

	function addDir() {
		if (newDir && !directories.includes(newDir)) {
			directories = [...directories, newDir];
			newDir = '';
		}
	}

	function removeDir(dir: string) {
		directories = directories.filter(d => d !== dir);
	}

	function getStorageConfig(): Record<string, unknown> {
		switch (storageProvider) {
			case 'b2': return { type: 'b2', key_id: b2KeyId, app_key: b2AppKey, bucket: b2Bucket };
			case 's3': return { type: 's3', endpoint: s3Endpoint, access_key: s3AccessKey, secret_key: s3SecretKey, bucket: s3Bucket, region: s3Region };
			case 'wasabi': return { type: 'wasabi', access_key: wasabiAccessKey, secret_key: wasabiSecretKey, bucket: wasabiBucket, region: wasabiRegion };
			case 'dropbox': return { type: 'dropbox', token: dropboxToken };
			case 'gdrive': return { type: 'gdrive', token: gdriveToken };
			case 'sftp': return { type: 'sftp', host: sftpHost, port: sftpPort, username: sftpUsername, password: sftpPassword, remote_path: sftpPath };
			case 'local': return { type: 'local', path: localPath };
			default: return {};
		}
	}

	function isStorageValid(): boolean {
		switch (storageProvider) {
			case 'b2': return !!(b2KeyId && b2AppKey && b2Bucket);
			case 's3': return !!(s3AccessKey && s3SecretKey && s3Bucket);
			case 'wasabi': return !!(wasabiAccessKey && wasabiSecretKey && wasabiBucket);
			case 'dropbox': return !!dropboxToken;
			case 'gdrive': return !!gdriveToken;
			case 'sftp': return !!(sftpHost && sftpUsername);
			case 'local': return !!localPath;
			default: return false;
		}
	}

	async function completeSetup() {
		if (encryptionPassword !== confirmPassword) {
			return addToast({ type: 'error', message: 'Passwords do not match' });
		}
		if (encryptionPassword.length < 12) {
			return addToast({ type: 'error', message: 'Password must be at least 12 characters' });
		}

		loading = true;
		try {
			const result = await api.completeSetup({
				encryption_password: encryptionPassword,
				storage: getStorageConfig(),
				db_dump_schedule: dbDumpSchedule,
				cloud_sync_schedule: cloudSyncSchedule,
				flash_schedule: flashSchedule,
				directories,
			});
			generatedKey = result.api_key;
			applySession({
				setup_required: false,
				authenticated: true,
				setup_completed_at: result.setup_completed_at,
			});
			step = 6;
			// Clear persisted step on completion
			if (typeof sessionStorage !== 'undefined') {
				sessionStorage.removeItem('arkive_setup_step');
			}
		} catch (e: any) {
			addToast({ type: 'error', message: e.message || 'Setup failed' });
		}
		loading = false;
	}

	function finish() {
		setupCompleted.set(true);
		goto('/');
	}
</script>

<div class="min-h-screen bg-page flex items-center justify-center p-6">
	<div class="w-full max-w-xl">
		<!-- Logo -->
		<div class="flex items-center gap-3 mb-8 justify-center">
			<div class="w-10 h-10 rounded-lg bg-primary flex items-center justify-center">
				<span class="text-white font-bold text-lg">A</span>
			</div>
			<div>
				<h1 class="text-xl font-bold text-text">Arkive Setup</h1>
				<p class="text-xs text-text-secondary">Automated Disaster Recovery</p>
			</div>
		</div>

		<!-- Progress -->
		<div class="flex items-center gap-2 mb-8 justify-center">
			{#each [1, 2, 3, 4, 5, 6] as s}
				<div class="flex items-center gap-2">
					<div class="w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium {
						step >= s ? 'bg-primary text-white' : 'bg-surface text-text-secondary border border-border'
					}">{s}</div>
					{#if s < 6}
						<div class="w-6 h-0.5 {step > s ? 'bg-primary' : 'bg-border'}"></div>
					{/if}
				</div>
			{/each}
		</div>

		<div class="card">
			{#if step === 1}
				<!-- Step 1: Encryption Password -->
				<h2 class="text-lg font-semibold text-text mb-2">Encryption Password</h2>
				<p class="text-sm text-text-secondary mb-6">This password encrypts all your backup data. Store it securely — if lost, backups cannot be restored.</p>
				<div class="space-y-4">
					<div>
						<label for="enc-password" class="block text-sm text-text-secondary mb-1">Password (min 12 characters)</label>
						<input id="enc-password" type="password" bind:value={encryptionPassword} class="input" placeholder="Enter a strong password" />
					</div>
					<div>
						<label for="confirm-password" class="block text-sm text-text-secondary mb-1">Confirm Password</label>
						<input id="confirm-password" type="password" bind:value={confirmPassword} class="input" placeholder="Confirm password" />
					</div>
				</div>
				<div class="mt-6 flex justify-end">
					<button on:click={() => step = 2} disabled={encryptionPassword.length < 12 || encryptionPassword !== confirmPassword} class="btn-primary disabled:opacity-50">Next</button>
				</div>

			{:else if step === 2}
				<!-- Step 2: Container Discovery -->
				<h2 class="text-lg font-semibold text-text mb-2">Container Discovery</h2>
				<p class="text-sm text-text-secondary mb-4">Arkive can scan your Docker containers to automatically discover databases for backup.</p>

				<div class="bg-warning/10 border border-warning/30 rounded-lg p-3 mb-6">
					<p class="text-sm text-text flex items-start gap-2">
						<svg class="w-5 h-5 text-warning shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
						<span>Arkive uses read-only Docker socket access to discover containers.</span>
					</p>
				</div>

				{#if !scanDone}
					<div class="text-center py-4">
						<button on:click={scanContainers} disabled={scanning} class="btn-primary disabled:opacity-50">
							{#if scanning}
								<svg class="animate-spin -ml-1 mr-2 h-4 w-4 text-white inline" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>
								Scanning...
							{:else}
								Scan for Containers
							{/if}
						</button>
					</div>
				{:else if scanError}
					<div class="bg-surface rounded-lg border border-border p-4 text-center">
						<svg class="w-8 h-8 text-text-secondary mx-auto mb-2" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" /></svg>
						<p class="text-sm text-text-secondary mb-1">{scanError}</p>
						<p class="text-xs text-text-secondary">You can configure databases manually from the dashboard after setup.</p>
					</div>
				{:else if containers.length > 0}
					<div class="space-y-2 max-h-64 overflow-y-auto">
						{#each containers as c}
							<div class="flex items-center gap-3 bg-surface rounded-lg border border-border p-3">
								<div class="w-8 h-8 rounded bg-primary/10 flex items-center justify-center shrink-0">
									<svg class="w-4 h-4 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2" /></svg>
								</div>
								<div class="flex-1 min-w-0">
									<div class="text-sm font-medium text-text truncate">{c.name}</div>
									<div class="text-xs text-text-secondary truncate">{c.image}</div>
								</div>
								{#if c.databases && c.databases.length > 0}
									<span class="text-xs bg-success/15 text-success px-2 py-0.5 rounded-full shrink-0">{c.databases.length} DB{c.databases.length > 1 ? 's' : ''}</span>
								{:else if c.has_database}
									<span class="text-xs bg-success/15 text-success px-2 py-0.5 rounded-full shrink-0">DB</span>
								{/if}
							</div>
						{/each}
					</div>
					<p class="text-xs text-text-secondary mt-3">Found {containers.length} container{containers.length !== 1 ? 's' : ''}. Databases will be automatically backed up.</p>
				{:else}
					<div class="bg-surface rounded-lg border border-border p-4 text-center">
						<p class="text-sm text-text-secondary">No containers found. You can add databases manually from the dashboard.</p>
					</div>
				{/if}

				<div class="mt-6 flex justify-between">
					<button on:click={() => step = 1} class="btn-secondary">Back</button>
					<button on:click={() => step = 3} class="btn-primary">{scanDone ? 'Next' : 'Skip'}</button>
				</div>

			{:else if step === 3}
				<!-- Step 3: Choose Storage Destination (BYOS) -->
				<h2 class="text-lg font-semibold text-text mb-2">Backup Destination</h2>
				<p class="text-sm text-text-secondary mb-6">Where should Arkive store your encrypted backups? Choose a storage provider you control.</p>

				<div class="grid grid-cols-2 gap-2 mb-6">
					{#each providers as p}
						<button
							on:click={() => storageProvider = p.value}
							class="flex items-start gap-3 p-3 rounded-lg border text-left transition-all {
								storageProvider === p.value
									? 'border-primary bg-primary/5 ring-1 ring-primary/20'
									: 'border-border hover:border-text-secondary/30'
							}"
						>
							<span class="text-xl mt-0.5">{p.icon}</span>
							<div>
								<div class="text-sm font-medium text-text">{p.label}</div>
								<div class="text-xs text-text-secondary leading-snug">{p.desc}</div>
							</div>
						</button>
					{/each}
				</div>

				<!-- Provider-specific credential fields -->
				{#if storageProvider === 'b2'}
					<div class="space-y-3 border-t border-border pt-4">
						<div>
							<label for="b2-key-id" class="block text-sm text-text-secondary mb-1">Key ID</label>
							<input id="b2-key-id" type="text" bind:value={b2KeyId} class="input font-mono" placeholder="00123456789abcdef0000000000" />
						</div>
						<div>
							<label for="b2-app-key" class="block text-sm text-text-secondary mb-1">Application Key</label>
							<input id="b2-app-key" type="password" bind:value={b2AppKey} class="input font-mono" />
						</div>
						<div>
							<label for="b2-bucket" class="block text-sm text-text-secondary mb-1">Bucket Name</label>
							<input id="b2-bucket" type="text" bind:value={b2Bucket} class="input font-mono" placeholder="my-arkive-backups" />
						</div>
					</div>

				{:else if storageProvider === 's3'}
					<div class="space-y-3 border-t border-border pt-4">
						<div>
							<label for="s3-endpoint" class="block text-sm text-text-secondary mb-1">Endpoint URL</label>
							<input id="s3-endpoint" type="text" bind:value={s3Endpoint} class="input font-mono" placeholder="https://s3.us-east-1.amazonaws.com" />
						</div>
						<div class="grid grid-cols-2 gap-3">
							<div>
								<label for="s3-access-key" class="block text-sm text-text-secondary mb-1">Access Key</label>
								<input id="s3-access-key" type="text" bind:value={s3AccessKey} class="input font-mono" />
							</div>
							<div>
								<label for="s3-secret-key" class="block text-sm text-text-secondary mb-1">Secret Key</label>
								<input id="s3-secret-key" type="password" bind:value={s3SecretKey} class="input font-mono" />
							</div>
						</div>
						<div class="grid grid-cols-2 gap-3">
							<div>
								<label for="s3-bucket" class="block text-sm text-text-secondary mb-1">Bucket</label>
								<input id="s3-bucket" type="text" bind:value={s3Bucket} class="input font-mono" placeholder="my-backups" />
							</div>
							<div>
								<label for="s3-region" class="block text-sm text-text-secondary mb-1">Region</label>
								<input id="s3-region" type="text" bind:value={s3Region} class="input font-mono" placeholder="us-east-1" />
							</div>
						</div>
					</div>

				{:else if storageProvider === 'wasabi'}
					<div class="space-y-3 border-t border-border pt-4">
						<div class="grid grid-cols-2 gap-3">
							<div>
								<label for="wasabi-access-key" class="block text-sm text-text-secondary mb-1">Access Key</label>
								<input id="wasabi-access-key" type="text" bind:value={wasabiAccessKey} class="input font-mono" />
							</div>
							<div>
								<label for="wasabi-secret-key" class="block text-sm text-text-secondary mb-1">Secret Key</label>
								<input id="wasabi-secret-key" type="password" bind:value={wasabiSecretKey} class="input font-mono" />
							</div>
						</div>
						<div class="grid grid-cols-2 gap-3">
							<div>
								<label for="wasabi-bucket" class="block text-sm text-text-secondary mb-1">Bucket</label>
								<input id="wasabi-bucket" type="text" bind:value={wasabiBucket} class="input font-mono" placeholder="my-backups" />
							</div>
							<div>
								<label for="wasabi-region" class="block text-sm text-text-secondary mb-1">Region</label>
								<input id="wasabi-region" type="text" bind:value={wasabiRegion} class="input font-mono" placeholder="us-east-1" />
							</div>
						</div>
					</div>

				{:else if storageProvider === 'dropbox'}
					<div class="space-y-3 border-t border-border pt-4">
						<p class="text-xs text-text-secondary">Generate an access token from the <a href="https://www.dropbox.com/developers/apps" target="_blank" rel="noopener" class="text-primary hover:underline">Dropbox App Console</a>.</p>
						<div>
							<label for="dropbox-token" class="block text-sm text-text-secondary mb-1">OAuth Token</label>
							<input id="dropbox-token" type="password" bind:value={dropboxToken} class="input font-mono" />
						</div>
					</div>

				{:else if storageProvider === 'gdrive'}
					<div class="space-y-3 border-t border-border pt-4">
						<p class="text-xs text-text-secondary">Use <code class="bg-surface px-1 rounded">rclone authorize "drive"</code> to generate a token, then paste it here.</p>
						<div>
							<label for="gdrive-token" class="block text-sm text-text-secondary mb-1">OAuth Token</label>
							<input id="gdrive-token" type="password" bind:value={gdriveToken} class="input font-mono" />
						</div>
					</div>

				{:else if storageProvider === 'sftp'}
					<div class="space-y-3 border-t border-border pt-4">
						<div class="grid grid-cols-3 gap-3">
							<div class="col-span-2">
								<label for="sftp-host" class="block text-sm text-text-secondary mb-1">Host</label>
								<input id="sftp-host" type="text" bind:value={sftpHost} class="input font-mono" placeholder="192.168.1.100" />
							</div>
							<div>
								<label for="sftp-port" class="block text-sm text-text-secondary mb-1">Port</label>
								<input id="sftp-port" type="number" bind:value={sftpPort} class="input font-mono" />
							</div>
						</div>
						<div class="grid grid-cols-2 gap-3">
							<div>
								<label for="sftp-username" class="block text-sm text-text-secondary mb-1">Username</label>
								<input id="sftp-username" type="text" bind:value={sftpUsername} class="input font-mono" />
							</div>
							<div>
								<label for="sftp-password" class="block text-sm text-text-secondary mb-1">Password</label>
								<input id="sftp-password" type="password" bind:value={sftpPassword} class="input font-mono" />
							</div>
						</div>
						<div>
							<label for="sftp-path" class="block text-sm text-text-secondary mb-1">Remote Path</label>
							<input id="sftp-path" type="text" bind:value={sftpPath} class="input font-mono" placeholder="/backups/arkive" />
						</div>
					</div>

				{:else if storageProvider === 'local'}
					<div class="space-y-3 border-t border-border pt-4">
						<div>
							<label for="local-path" class="block text-sm text-text-secondary mb-1">Local Path</label>
							<input id="local-path" type="text" bind:value={localPath} class="input font-mono" placeholder="/mnt/user/backups" />
						</div>
						<p class="text-xs text-text-secondary">Can be a local directory or a mounted network share (SMB/NFS).</p>
					</div>

				{/if}

				<div class="mt-6 flex justify-between">
					<button on:click={() => step = 2} class="btn-secondary">Back</button>
					<button on:click={() => step = 4} disabled={!storageValid} class="btn-primary disabled:opacity-50">Next</button>
				</div>

			{:else if step === 4}
				<!-- Step 4: Schedule -->
				<h2 class="text-lg font-semibold text-text mb-2">Backup Schedule</h2>
				<p class="text-sm text-text-secondary mb-6">Configure when backups run. Each schedule shows upcoming run times so you do not have to read raw cron.</p>
				<div class="space-y-4">
					<div>
						<CronEditor bind:value={dbDumpSchedule} label="Database Dumps" />
					</div>
					<div>
						<CronEditor bind:value={cloudSyncSchedule} label="Cloud Sync" />
					</div>
					<div>
						<CronEditor bind:value={flashSchedule} label="Flash Backup (Unraid)" />
					</div>
				</div>
				<div class="mt-6 flex justify-between">
					<button on:click={() => step = 3} class="btn-secondary">Back</button>
					<button on:click={() => step = 5} class="btn-primary">Next</button>
				</div>

			{:else if step === 5}
				<!-- Step 5: Directories -->
				<h2 class="text-lg font-semibold text-text mb-2">Directories to Watch</h2>
				<p class="text-sm text-text-secondary mb-6">Add directories to include in backups. Arkive will also auto-discover container volumes.</p>
				{#if setupCoverageHint}
					<div class="mb-4 rounded-lg border border-success/30 bg-success/10 px-4 py-3 text-sm text-text-secondary">
						{setupCoverageHint}
					</div>
				{:else if directoryScanPlatform === 'unraid'}
					<div class="mb-4 rounded-lg border border-warning/30 bg-warning/10 px-4 py-3 text-sm text-text-secondary">
						For full Unraid recovery, include <span class="font-mono text-text">/mnt/user/appdata</span>. Database dumps and flash backup alone are not enough for an identical server rebuild.
					</div>
				{/if}
				<div class="flex gap-2 mb-4">
					<label for="new-dir" class="sr-only">Directory path</label>
					<input id="new-dir" type="text" bind:value={newDir} class="input" placeholder="/mnt/user/appdata" on:keydown={(e) => e.key === 'Enter' && addDir()} />
					<button on:click={addDir} class="btn-primary shrink-0">Add</button>
				</div>
				{#if directories.length > 0}
					<div class="space-y-2 mb-4">
						{#each directories as dir}
							<div class="flex items-center justify-between bg-surface-hover rounded px-3 py-2">
								<span class="text-sm font-mono text-text">{dir}</span>
								<button on:click={() => removeDir(dir)} class="text-text-secondary hover:text-danger" aria-label="Remove directory {dir}">
									<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
								</button>
							</div>
						{/each}
					</div>
				{/if}
				<div class="mt-6 flex justify-between">
					<button on:click={() => step = 4} class="btn-secondary">Back</button>
					<button on:click={completeSetup} disabled={loading} class="btn-primary disabled:opacity-50">
						{loading ? 'Setting up...' : 'Complete Setup'}
					</button>
				</div>

			{:else if step === 6}
				<!-- Step 6: Done -->
				<div class="text-center">
					<div class="w-12 h-12 rounded-full bg-success/15 flex items-center justify-center mx-auto mb-4">
						<svg class="w-6 h-6 text-success" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" /></svg>
					</div>
					<h2 class="text-lg font-semibold text-text mb-2">Setup Complete!</h2>
					<p class="text-sm text-text-secondary mb-6">Save your API key below. It won't be shown again.</p>
					<div class="bg-page border border-border rounded p-4 mb-6">
						<p class="text-xs text-text-secondary mb-2">API Key</p>
						<code class="text-sm text-primary break-all font-mono">{generatedKey}</code>
					</div>
					<button on:click={finish} class="btn-primary w-full">Go to Dashboard</button>
				</div>
			{/if}
		</div>
	</div>
</div>
