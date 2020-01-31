"""Converts unprocessed ilab requests to Clarity projects."""
import os
import argparse
import logging
import traceback
import datetime
import requests
from collections import namedtuple
from bs4 import BeautifulSoup
from dataclasses import dataclass, field
from ua_ilab_tools import ua_ilab_tools, api_types
from ua_project_transfer import core_specifics
from ua_project_transfer import price_check


def setup_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ilab", dest="ilab_env", required=True)
    parser.add_argument(
        "--lims", dest="lims_env", required=True)

    return parser.parse_args()


# Set up log.
LOGGER = logging.getLogger(__name__)
core_specifics.setup_log()

# Set up script monitoring to be notified if it unexpectedly breaks.
core_specifics.setup_monitoring()

# Set up the arguments with names for the different environments.
ARGS = setup_arguments()

# Setup tools and apis.
LIMS_UTILITY = core_specifics.setup_lims_api(ARGS.lims_env)
ILAB_TOOLS = core_specifics.setup_ilab_api(ARGS.ilab_env)
CLARITY_API = core_specifics.setup_clarity_api(ARGS.lims_env)

# Set up Sample tuple that holds necessary information for comparing samples.
Sample = namedtuple("Sample", ["name", "location", "container"])


@dataclass
class ProjectRecord():
    """Holds the information required to log a project."""
    uri: str
    expected_price: str = ""
    actual_price: str = ""
    date: datetime.datetime = field(default_factory=datetime.datetime.now)


@dataclass
class ProjectData():
    """Holds the common information for each project."""
    req_id: str
    current_form: api_types.CustomForm = None
    current_record: ProjectRecord = None
    req_type: str = ""
    prj_info: api_types.Project = None


def update_logger(request_type):
    """Update logging information to inform the correct people.

    Arguments:
        request_type (string):
            The type of request that was specified in iLab."""
    try:
        warn_index = LOGGER.handlers.index("warn_handler")
        err_index = LOGGER.handlers.index("error_handler")
        LOGGER.handlers[warn_index].toaddrs = core_specifics.get_email(
            request_type)
        LOGGER.handlers[err_index].toaddrs = core_specifics.get_email(
            request_type)
    except (ValueError, AttributeError):
        # Do nothing if the log has not been configured.
        pass


def harvest_form(prj_data):
    """Get the correct form from iLab.

    Arguments:
        prj_data (dataclass):
            Object that holds important information for transferring projects.

    Returns:
        custom_form (api_types.CustomForm):
            Object that is populated with all of the information gathered
            from the iLab form.
            None if nothing is found.
    """
    try:
        forms_uri_to_soup = ILAB_TOOLS.get_custom_forms(prj_data.req_id)
    except ua_ilab_tools.IlabConfigError:
        # Requests without forms are Consultation Requests or Custom Projects
        # that have not yet had a business form attached, and so the request
        # should just be skipped.
        return None
    # Make sure the current_form is zeroed out between request id's.
    else:
        current_form = None

    # Get all of the custom forms for the current req_id.
    for form_uri, form_soup in forms_uri_to_soup.items():
        # Get the form info (including the sample info) before you post
        # anything, as this is where most of the errors are thrown.
        try:
            current_form = ua_ilab_tools.extract_custom_form_info(
                prj_data.req_id, form_uri, form_soup)

        except TypeError as grid_error:
            grid_error = str(grid_error).replace('"', '\'')
            LOGGER.error({
                "template": os.path.join("project_transfer", "error.html"),
                "content": (
                    f"The request {prj_data.current_record} has been filled"
                    f" out incorrectly. The error message is:\n{grid_error}")
            })
            break

        # Add the request_type to the form that was just added.
        current_form.request_type = prj_data.request_type

        # If the request only has a 'Request a Quote' form, continue.
        if "REQUEST A QUOTE" in current_form.name.strip().upper():
            current_form = None
            continue

    return current_form


def samples_differ(prj_smp_limsids, curr_form):
    """Finds the difference in samples between iLab and Clarity.

    Arguments:
        prj_smp_limsids (list of strings):
            The sample limsids of the samples found in the current Clarity
            project.
        curr_form (api_types.CustomForm):
            Object containing information relevant to the project from iLab."""
    # If the Clarity project has no samples, make an empty list.
    if len(prj_smp_limsids) == 0:
        prj_smp_names = list()
    # Otherwise get the samples that are in the project.
    else:
        prj_smp_names = get_clarity_samples(prj_smp_limsids)

    # Keep only the samples in the current_form that aren't already in Clarity.
    samples_to_add = list()
    for sample in curr_form.samples:
        info = Sample(sample.name, sample.location, sample.con.name)
        if info not in prj_smp_names:
            samples_to_add.append(sample)
    curr_form.samples = samples_to_add


def get_clarity_samples(prj_smp_limsids):
    """Harvest Clarity sample information from previously made project.

    Arguments:
        prj_smp_limsids (list of strings):
            The sample limsids of the samples found in the current Clarity
            project.

    Returns:
        prj_smp_names (list of Sample namedtuples):
            List of the relevant information for comparing samples from the
            sample in Clarity."""
    # Get a list of artifacts based on sample limsids.
    prj_smps = BeautifulSoup(
        CLARITY_API.get(
            f"{CLARITY_API.host}artifacts",
            parameters={"samplelimsid": prj_smp_limsids}),
        "xml")

    # Get a list of the artifact uris.
    art_uris = [art["uri"] for art in prj_smps.find_all("artifact")]
    # Get each of the artifacts.
    art_soup = BeautifulSoup(CLARITY_API.get(art_uris), "xml")

    # Extract info from the artifacts into tuples.
    prj_smp_info = list()
    for art in art_soup.find_all("artifact"):
        # Get the name, location, and container uri.
        smp_name = art.find("name").text
        smp_loc_tag = art.find("location")
        smp_loc_val = smp_loc_tag.find("value").text
        smp_con_uri = smp_loc_tag.find("container")["uri"]
        prj_smp_info.append(Sample(smp_name, smp_loc_val, smp_con_uri))

    # Make a set of each container uri that needs to be gotten.
    con_uris = {sample.container for sample in prj_smp_info}
    # Get the containers.
    con_soup = BeautifulSoup(CLARITY_API.get(list(con_uris)), "xml")

    # Make a dictionary of con uri to con name.
    con_uri_name = dict()
    for con in con_soup.find_all("con:container"):
        con_uri_name[con["uri"]] = con.find("name").text

    # Update the sample info tuples to have con name.
    prj_smp_names = list()
    for smp in prj_smp_info:
        smp_con_name = con_uri_name[smp.container]
        prj_smp_names.append(Sample(smp.name, smp.location, smp_con_name))

    return prj_smp_names


def post_project(prj_data, new_proj):
    """Posts everything required for Clarity and routes samples to workflows.

    Arguments:
        prj_data (ProjectData):
            Object containing all of the other information necessary for
            posting a new Clarity project.
        new_proj (boolean):
            True: Creates a new Clarity project and price checks it.
            False: Posts everything but the Clarity project and doesn't price
                check."""
    # Post the everything besides the project.
    delete_list = list()
    try:
        sample_uris = create_clarity_objs(prj_data, delete_list, new_proj)
    except BaseException as post_error:
        LOGGER.error({
            "template": os.path.join("project_transfer", "error.html"),
            "content": (
                f"The request {prj_data.current_record} has been filled out"
                f" incorrectly. The error message is:\n {post_error}")
        })
        # Delete the posted items if an error occurs.
        delete_items(delete_list)
    else:
        if new_proj:
            get_price(prj_data)
        # Route them to respective workflows.
        route_samples(sample_uris, prj_data)


def create_clarity_objs(prj_data, delete_list, new_proj):
    """Post new project/sample information.

    Arguments:
        prj_data (ProjectData):
            Object containing all of the other information necessary for
            posting a new Clarity Project.
        delete_list (list or strings):
            When new Clarity objects are posted, their uris are added to the
            list to be deleted in case a future posting fails.
        new_proj (boolean):
            True: Posts a new Clarity project with everything else.
            False: Doesn't post a new Clarity project, only the other objects.

    Returns:
        sample_uris (list of strings):
            List of Clarity sample uris that were posted.

    Side Effects:
        Creates new objects in Clarity.
    """
    res_uri = LIMS_UTILITY.create_researcher(
        prj_data.req_id, prj_data.prj_info)
    delete_list.append(res_uri)

    if new_proj:
        prj_uri = LIMS_UTILITY.create_project(
            prj_data.req_id, prj_data.prj_info)
        delete_list.append(prj_uri)

    con_uris = LIMS_UTILITY.create_containers(
        prj_data.req_id, prj_data.prj_info, prj_data.current_form)
    delete_list.extend(con_uris)

    sample_uris = LIMS_UTILITY.create_samples(
        prj_data.req_id, prj_data.prj_info, prj_data.current_form)
    delete_list.extend(sample_uris)

    return sample_uris


def delete_items(delete_list):
    """ Delete items from Clarity in allowed order.

    Arguments:
        delete_list (list of strings):
            Strings of Clarity object uris to delete."""
    for item in delete_list[::-1]:
        try:
            CLARITY_API.delete(item)
        except requests.exceptions.HTTPError:
            continue


def get_price(prj_data):
    """Gets the price from iLab and updates the ProjectData.

    Arguments:
        prj_data (ProjectData):
            Object containing all of the other necessary information for
            posting a Clarity project."""
    # Setup the charge pricing scheme.
    charge_per_reaction = {
        "Transgenic Mouse Genotyping": [
            "TGM Assay List",
            "TGM Other Assay List"],
        "Low Volume Sequencing": ["Primer"]}

    # Collect and compare the expected and actual prices.
    try:
        if prj_data.request_type in charge_per_reaction.keys():
            req_price, calcd_price = price_check.check_request(
                ILAB_TOOLS,
                prj_data.req_id,
                prj_data.req_type,
                prj_data.current_form.samples,
                rxn_multiplier=charge_per_reaction[prj_data.request_type])
        else:
            req_price, calcd_price = price_check.check_request(
                ILAB_TOOLS,
                prj_data.req_id,
                prj_data.request_type,
                prj_data.current_form.samples)

        prj_data.current_record.actual_price = req_price
        prj_data.current_record.expected_price = calcd_price

    # Catch BaseException here because I don't want the program to ever stop
    # here if price_check throws an error.
    except BaseException:
        LOGGER.warning({
            "template": os.path.join("project_transfer", "price_warning.html"),
            "content": (
                f"The project {prj_data.req_id} prices could not be checked."
                f" The traceback is:\n{traceback.format_exc()}")
        })


def route_samples(sample_uris, prj_data):
    """ Route the newly posted samples to their respective workflows.

    Arguments:
        sample_uris (list of strings):
            List of Clarity sample uris to route to their respective workflows.

    Side Effects:
        If successful, attaches the samples in Clarity to the first step in
        whichever workflow is specified in wf_steps."""
    smp_art_uris = (
        LIMS_UTILITY.lims_api.tools.get_arts_from_samples(sample_uris))

    # Assign those samples to workflows.
    try:
        LIMS_UTILITY.route_strategy(
            smp_art_uris.values(), prj_data.current_form, ARGS.lims_env)
    # Catch BaseException here because I don't want the program to ever stop
    # here if route_strategy throws an error.
    except BaseException:
        LOGGER.warning({
            "template": os.path.join("project_transfer", "route_warning.html"),
            "content": (
                f"The project {prj_data.req_id} could not be routed. The"
                f" traceback is:\n{traceback.format_exc()}")
        })


def main():
    try:
        all_req_ids = ILAB_TOOLS.get_service_requests(status="processing")
    except RuntimeError:
        # No Requests to process.
        all_req_ids = {}

    # Get a list of the iLab form names. Sort them from smallest -> largest.
    to_process = sorted(list(all_req_ids.keys()))

    # Query Clarity for all of the projects in to_process.
    clarity_projects = BeautifulSoup(CLARITY_API.get(
        f"{CLARITY_API.host}projects", parameters={"name": to_process}), "xml")

    # Make a dictionary mapping project name to project limsid.
    clarity_prj_ids = {prj.find("name").text: prj[
        "limsid"] for prj in clarity_projects.find_all("project")}

    for req_id in to_process:
        prj_data = ProjectData(req_id=req_id)
        # Gather all of the project information from iLab.
        req_soup = all_req_ids[req_id]
        request_type = req_soup.find("category-name").string
        prj_data.request_type = request_type

        update_logger(request_type)

        # Get project information from iLab.
        prj_info = ua_ilab_tools.extract_project_info(req_soup)
        prj_data.prj_info = prj_info
        current_record = ProjectRecord(uri=prj_info.name)
        prj_data.current_record = current_record

        # Get the custom form from iLab.
        current_form = harvest_form(prj_data)
        # If there was an error with the business form, don't try to post info
        # that is not there.
        if current_form is None:
            continue
        prj_data.current_form = current_form

        # If the project was already in Clarity, compare the samples.
        if req_id in clarity_prj_ids.keys():
            # Get the current samples in Clarity's project.
            prj_smps = BeautifulSoup(
                CLARITY_API.get(
                    f"{CLARITY_API.host}samples",
                    parameters={"projectlimsid": clarity_prj_ids[req_id]}),
                "xml")
            prj_smp_limsids = [
                sample["limsid"] for sample in prj_smps.find_all("sample")]

            # Check if the samples differ between Clarity and iLab.
            if len(current_form.samples) != len(prj_smp_limsids):
                # Update prj_info to have the already made project uri.
                prj_info.uri = (
                    f"{CLARITY_API.host}projects/"f"{clarity_prj_ids[req_id]}")

                # Update current_form's samples to contain samples that need to
                # be posted.
                samples_differ(prj_smp_limsids, current_form)

                # Post the samples without a new project.
                post_project(prj_data, False)
        else:
            # Post the project and get its price.
            post_project(prj_data, True)


if __name__ == '__main__':
    main()
