import subprocess

# scripts to run from the command line

folder_names = [ 
    'industrial'
    ]


scripts = [
    ('01_tag.py', []),
    ('02_rename.py', []),
    ('03_folders.py', []),
    ('04_move.py', []),
]

def run_step(script_name: str, args: list[str]) -> int:
    """Run a step script with the given arguments. Returns the exit code."""
    result = subprocess.run(['python', script_name] + args, capture_output=True, text=True)
    return result

for folder in folder_names:
    print(f"\nProcessing folder: {folder}")
    
    for script_name, extra_args in scripts:
        print(f"\n--- {script_name} ---")
        args = ['--dry-run', '--folder', folder] + extra_args
        
        # Run in dry-run mode
        result = run_step(script_name, args)
        print(result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr)
        
        # Prompt user to run live
        if input(f"Run {script_name} live? (y/n): ").lower() == 'y':
            live_args = ['--no-dry-run', '--folder', folder] + extra_args
            result = run_step(script_name, live_args)
            print(result.stdout)
        else:
            print(f"Skipped {script_name}")