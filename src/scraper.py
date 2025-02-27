import asyncio
import getpass
import os
import secrets
import shutil
import tempfile
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Coroutine, Iterable

import httpx
from bs4 import BeautifulSoup
from bs4.element import SoupStrainer, Tag
from sanitize_filename import sanitize

import utils


class Scraper:
    ONLY_TABLE = SoupStrainer("table")
    ONLY_MAIN_CONTENT = SoupStrainer("div", {"class": "page-content inset"})
    ONLY_SETTINGS = SoupStrainer("form", attrs={"name": "user_settings"})

    original_settings = {}

    def __new__(cls, *args, **kwargs):
        self = super().__new__(cls)
        self._args = args
        self._kwargs = kwargs
        return self

    def __await__(self):
        yield from self._scraper_init(*self._args, **self._kwargs).__await__()
        return self

    async def _scraper_init(
        self,
        client: httpx.AsyncClient = None,
        url: str = None,
        username: str = None,
        password: str = None,
        filters: Iterable[str] = None,
    ) -> None:
        """
        _scraper_init initialize Scraper

        Args:
            client (httpx.AsyncClient, optional): a custom client. Defaults to None.
            url (str, optional): a different url to a IServ instance. Defaults to None.
            username (str, optional): username. Defaults to None.
            password (str, optional): password. Defaults to None.
            filters (Iterable[str], optional): list of filters to exclude exercises. Defaults to None.
        """
        self.url = url if url is not None else "https://whg-duew.de"
        self.client = (
            client
            if client is not None
            else httpx.AsyncClient(
                base_url=self.url, headers={"user-agent": "IServ-exercise-scraper/1.0"}
            )
        )
        self.username = username
        self.password = password
        self.filters = filters if filters is not None else []

    async def prompt_login(self):
        """
        prompt_login prompt the user to login in,
            also makes sure that username and password are correct.
        """
        logged_in = False
        self.username = (
            self.username if self.username is not None else input("Username: ")
        )
        self.password = (
            self.password if self.password is not None else getpass.getpass()
        )
        while not logged_in:
            logged_in = await self.login()
            if not logged_in:
                print("Passwort oder Benutzername falsch.")
                self.username, self.password = input("Username: "), getpass.getpass()

        print("Erfolgreich eingeloggt!")

    async def login(self) -> bool:
        """
        Log the user in.
        Returns:
            True if login successful
            False if login failed
        Raises ConnectionError when some error occurred
        """
        login_path = "/iserv/app/login"
        login_info = {"_username": self.username, "_password": self.password}
        page_login = None
        while page_login is None:
            try:
                page_login = await self.client.post(login_path, data=login_info)
            except httpx.TimeoutException as e:
                print("Zeitüberschreitung während des Logins. Versuche es erneut...")
                continue

        if page_login.url.path.startswith(login_path):
            return False
        elif page_login.status_code != 200 and page_login.status_code != 302:
            await self.close()
            raise ConnectionError("Could not connect to server.")
        else:
            return True

    def href_filter(self, href: str):
        return href.startswith(self.url + "/iserv/exercise/show/")

    def tag_filter(self, tag: Tag) -> bool:
        if tag.name != "a":
            return False
        filters_result = True
        if tag.string and self.filters:
            filters_result = not any(ele in tag.string for ele in self.filters)
        return self.href_filter(tag["href"]) and filters_result

    async def change_language(self):
        """
        Change language of user to German to guarantee correct parsing.
        """
        settings_page = await self.client.get("/iserv/profile/settings")
        soup_settings_page = BeautifulSoup(
            settings_page.text, "html.parser", parse_only=self.ONLY_SETTINGS
        )
        data = {
            e["name"]: e.get("value", "")
            for e in soup_settings_page.find_all("input", {"name": True})
        }
        data.update(
            {
                e["name"]: e.find("option", selected=True).get("value", "")
                for e in soup_settings_page.find_all("select", {"name": True})
            }
        )
        self.original_settings = data.copy()
        data["user_settings[lang]"] = "de_DE"
        await self.client.post("/iserv/profile/settings", data=data)

    async def reset_language(self):
        await self.client.post("/iserv/profile/settings", data=self.original_settings)

    def convert_list_to_str(self, lst: list):
        return "".join(map(str, lst)).strip().strip("\n")

    async def extract_feedback(self, soup_page: BeautifulSoup):
        """
        extract_feedback extracts the feedback from an exercise page

        Args:
            soup_page (BeautifulSoup): parsed exercise page

        Returns:
            dict: extracted data
        """
        feedback = soup_page.find("div", string="Rückmeldungen")
        feedback_files = []
        if feedback:
            feedback_text_title = feedback.find_next("td", string="Rückmeldungstext")
            if feedback_text_title:
                feedback_text = self.convert_list_to_str(
                    feedback_text_title.parent.find_next(
                        "div", class_="text-break-word"
                    ).contents
                )
            else:
                feedback_text = "Kein Rückmeldungstext"

            feedback_files_title = feedback.find_next(
                "td", string="Rückmeldungs Dateien"
            )
            if feedback_files_title:
                feedback_files = list(
                    map(
                        lambda tag: tag["href"],
                        feedback_files_title.parent.parent.find_all(
                            "a", attrs={"target": "_blank", "class": "text-break-word"}
                        ),
                    )
                )
        else:
            feedback_text = "Keine Rückmeldung"

        data = {
            "Rückmeldungstext": feedback_text,
            "Rückmeldungsdateien": feedback_files,
        }

        return data

    async def extract_submission(self, soup_page: BeautifulSoup):
        """
        extract_submission extracts the submission data from an exercise

        Args:
            soup_page (BeautifulSoup): parsed exercise page

        Returns:
            dict: extracted data
        """
        submission_text_parent = soup_page.find(
            "form", attrs={"name": "submission"}
        ).find("h5", string="Deine Textabgabe")
        if submission_text_parent:
            submission_text = self.convert_list_to_str(
                submission_text_parent.find_next_sibling(
                    "div", class_="text-break-word"
                ).contents
            )
            submission_text = (
                submission_text if submission_text else "Kein Text abgegeben"
            )
        else:
            submission_text = "Kein Text abgegeben"

        submission_files_parent = soup_page.find("div", class_="panel-body pb-0")
        if submission_files_parent:
            if submission_files_parent.find("h5", string="Ihre abgegebenen Dateien"):
                submission_files = list(
                    map(
                        lambda tag: tag["href"],
                        submission_files_parent.find_all(
                            "a", attrs={"target": "_blank"}
                        ),
                    )
                )
        else:
            submission_files = []

        data = {
            "Abgabetext": submission_text,
            "Abgabedateien": submission_files,
        }
        return data

    async def extract_main_info(self, soup_page: BeautifulSoup):
        """
        extract_main_info extracts the main information from an exercise

        Args:
            soup_page (BeautifulSoup): parsed exercise page

        Returns:
            dict: extracted data
        """
        exercise_creator = (
            soup_page.find("th", string="Erstellt von:")
            .find_next_sibling("td")
            .contents[0]
            .string
        )
        exercise_description = self.convert_list_to_str(
            soup_page.find("div", string="Beschreibung:")
            .find_next_sibling("div")
            .contents
        )
        exercise_provided_files = []
        exercise_provided_files_parent = soup_page.find(
            "form", attrs={"name": "iserv_exercise_attachment"}
        )
        if exercise_provided_files_parent:
            exercise_provided_files = list(
                map(
                    lambda tag: tag["href"],
                    exercise_provided_files_parent.select("tr td a"),
                )
            )
        data = {
            "Lehrer": exercise_creator,
            "Beschreibung": exercise_description,
            "Bereitgestellte Dateien": exercise_provided_files,
        }
        return data

    async def __parse_exercise_page(self, link: str):
        """
        __parse_exercise_page parse an exercise page to extract additional information

        Args:
            link (str): link to the exercise page

        Returns:
            dict: extracted data
        """
        exercise_page = await self.client.get(link)
        soup_exercise_page = BeautifulSoup(
            exercise_page.text, "html.parser", parse_only=self.ONLY_MAIN_CONTENT
        )

        # Extract all data
        data_list = await asyncio.gather(
            self.extract_main_info(soup_exercise_page),
            self.extract_feedback(soup_exercise_page),
            self.extract_submission(soup_exercise_page),
        )
        data = {}
        for d in data_list:
            data.update(d)

        return data

    async def get_exercise_data(self, tag: Tag) -> dict:
        """
        get_exercise_data extracts all data for a specific exercise

        Args:
            tag (Tag): The a tag for a specific exercise from the exercise overview

        Returns:
            dict: all gathered data
        """
        link = tag["href"]
        parse_task = asyncio.create_task(self.__parse_exercise_page(link))
        exercise_name = tag.string
        exercise_info = list(tag.parent.next_siblings)
        start_date = datetime.strptime(exercise_info[0]["data-sort"], "%Y%m%d")
        end_date = datetime.strptime(exercise_info[1]["data-sort"], "%Y%m%d%H%M%S")
        exercise_tags = exercise_info[2].string
        exercise_data = {
            "Aufgabe": exercise_name,
            "Startdatum": start_date.strftime("%d.%m.%Y %H:%M"),
            "Enddatum": end_date.strftime("%d.%m.%Y %H:%M"),
            "Link": link,
            "Tags": exercise_tags,
        }

        parsed_data = await parse_task
        exercise_data.update(parsed_data)
        print(".", end="")
        return exercise_data

    async def _create_dir(self, path: Path):
        try:
            path.mkdir(parents=True)
        except FileExistsError:
            path = path / secrets.token_urlsafe(4)
            path.mkdir(parents=True)
        return path

    async def get_file(self, url: str, path: Path, iteration=0):
        """
        get_file download a file

        Args:
            url (str): url to download file from
            path (Path): directory to put file in
        """
        failed = False
        with open(path, "wb") as f:
            try:
                async with self.client.stream("GET", url) as response:
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            f.write(chunk)
            except httpx.TimeoutException as error:
                print("\nDatei konnte nicht heruntergeladen werden:", self.url + url)
                failed = True
        if failed:
            path.unlink()
            if iteration < 10:
                return self.get_file(url, path, iteration + 1)

    async def schedule_downloads(
        self, tasks: list[Coroutine], exercise_data: dict, key: str, dir: Path
    ):
        """
        schedule_downloads schedule downloads for all provided urls

        Args:
            tasks (list[Coroutine]): the list of tasks to add to
            exercise_data (dict): the data for the exercise
            key (str): the key for the urls
            dir (Path): the directory to put the files in

        Returns:
            list[Coroutine]: list of tasks to be executed
            dict: adjusted data
        """
        urls: list[str] = exercise_data[key]
        added_files = False
        if urls:
            url_dir = dir / key
            url_dir = await self._create_dir(url_dir)
            exercise_data[key] = ""
            for url in urls:
                filename = sanitize(os.path.basename(urllib.parse.unquote(url)))
                filepath = url_dir / filename
                tasks.append(self.get_file(url, filepath))
                exercise_data[key] += f"{filename}, "
            added_files = True
        else:
            exercise_data[key] = f"Keine {key}"
        return tasks, exercise_data, added_files

    async def get_exercise_files(self, exercise_data: dict, dir: Path):
        """
        get_exercise_files collects all files for an exercise

        Args:
            exercise_data (dict): the collected data for that exercise
            dir (Path): the directory to put the files into

        Returns:
            dict: the adjusted data
        """
        exercise_dir = dir / sanitize(exercise_data["Aufgabe"])
        exercise_dir = await self._create_dir(exercise_dir)
        tasks = []

        tasks, exercise_data, feedback_success = await self.schedule_downloads(
            tasks, exercise_data, "Rückmeldungsdateien", exercise_dir
        )
        tasks, exercise_data, submission_success = await self.schedule_downloads(
            tasks, exercise_data, "Abgabedateien", exercise_dir
        )
        tasks, exercise_data, provided_success = await self.schedule_downloads(
            tasks, exercise_data, "Bereitgestellte Dateien", exercise_dir
        )

        new_tasks = []

        while tasks:
            new_tasks = await asyncio.gather(*tasks)
            if new_tasks:
                tasks = list(filter(None, new_tasks))

        if not (feedback_success or submission_success or provided_success):
            exercise_dir.rmdir()
        print(".", end="")
        return exercise_data

    async def get_all_files(self, data: Iterable[dict]):
        """
        get_all_files iterates over all collected data to retrieve all files and collect them in a zip file

        Args:
            data (Iterable[dict]): the collected data

        Returns:
            Tuple[dict]: final processed data
        """
        print("\nDateien werden zusammengestellt...")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            exercises_dir = temp_path / "Aufgaben"
            exercises_dir = await self._create_dir(exercises_dir)
            tasks = [self.get_exercise_files(e_data, exercises_dir) for e_data in data]
            final_data = await asyncio.gather(*tasks)
            with open(temp_path / "daten.csv", "w", encoding="utf-8", newline="") as f:
                utils.create_csv(f, final_data)
            zipfile_name = (
                f"{datetime.now().strftime('%Y%m%d_%H%M')}_Aufgaben_{self.username}"
            )
            shutil.make_archive(zipfile_name, "zip", temp_dir)

    async def run(self):
        """
        Run the scraper and return the data.
        """
        await self.prompt_login()
        await self.change_language()
        page_exercises = await self.client.get("/iserv/exercise?filter[status]=all")
        soup_page_exercises = BeautifulSoup(
            page_exercises.text, "html.parser", parse_only=self.ONLY_TABLE
        )
        filtered_exercises = soup_page_exercises.find_all(self.tag_filter)
        tasks = [self.get_exercise_data(exercise) for exercise in filtered_exercises]
        print("Aufgaben Daten werden gesammelt...")
        data = await asyncio.gather(*tasks)
        await self.get_all_files(data)

    async def close(self):
        """
        Close connection and revert any setting changes.
        """
        await self.reset_language()
        await self.client.aclose()
        print("\nFertig.")


async def main():
    filter_config = "filters.cfg"
    mode = "r" if os.path.exists(filter_config) else "w"
    with open(filter_config, encoding="utf-8", mode=mode) as f:
        filters = [line.rstrip() for line in f]
    scraper = await Scraper(filters=filters)
    try:
        await scraper.run()
    finally:
        await scraper.close()


if __name__ == "__main__":
    asyncio.run(main())