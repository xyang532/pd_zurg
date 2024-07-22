from base import *
from utils.logger import *
from update.auto_update import BaseUpdate
from utils.download import download_and_extract, parse_repo_info


class PlexDebridUpdate(BaseUpdate):
    def start_process(self, process_name, config_dir="/", key_type=""):
        super().start_process(process_name, config_dir, ['python', './plex_debrid/main.py', '--config-dir', '/config'], key_type)
          
    def update_check(self):
        self.logger.info("Checking for available plex_debrid updates")
        
        try:
            username, repository, branch = parse_repo_info('PD_REPO')
        except ValueError as e:
            self.logger.error(str(e))
            return

        repo_url = f"https://github.com/{username}/{repository}/archive/refs/heads/{branch}.zip"
        settings_url = f"https://raw.githubusercontent.com/{username}/{repository}/{branch}/ui/ui_settings.py"
        self.logger.debug(f"Repository URL for plex_debrid update: {repo_url}")  
        self.logger.debug(f"Settings URL for plex_debrid update: {settings_url}") 
    
        with open('./config/settings.json', 'r') as f:
            json_data = load(f)
            version = json_data['version'][0]
            self.logger.info(f"Currently installed [v{version}]")

        try:
            response = requests.get(settings_url, timeout=5)
            response = response.content.decode('utf8')
            #self.logger.debug(f"Settings URL for plex_debrid update response: {response}") # Break in case of emergency
            if regex.search(r"(?<=')([0-9]+\.[0-9]+)(?=')", response):
                v = regex.search(r"(?<=')([0-9]+\.[0-9]+)(?=')", response).group()
                self.logger.debug(f"Latest version of plex_debrid: {v}")
                if float(version) < float(v):
                    download_release, error = download_and_extract(repo_url, './plex_debrid')
                    if not download_release:
                        self.logger.error(f"Failed to download update for plex_debrid: {error}")
                    else:    
                        self.logger.info(f"Automatic update installed for plex_debrid [v{v}]")
                        self.logger.info("Restarting plex_debrid")
                        if self.process:
                            self.process.kill()
                        self.start_process('plex_debrid')
                else:
                    self.logger.info("Automatic update not required for plex_debrid")
        except Exception as e:
            self.logger.error(f"Automatic update failed for plex_debrid: {e}")
