# Copyright (C) 2017-2025 University of Glasgow
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

# The module contains code to interact with the IETF Datatracker
# (https://datatracker.ietf.org/release/about)
#
# The Datatracker API is at https://datatracker.ietf.org/api/v1 and is
# a REST API implemented using Django Tastypie (http://tastypieapi.org)
#
# It's possible to do time range queries on many of these values, for example:
#   https://datatracker.ietf.org/api/v1/person/person/?time__gte=2018-03-27T14:07:36
#
# See also:
#   https://datatracker.ietf.org/api/
#   https://trac.tools.ietf.org/tools/ietfdb/wiki/DatabaseSchemaDescription
#   https://trac.tools.ietf.org/tools/ietfdb/wiki/DatatrackerDrafts
#   RFC 6174 "Definition of IETF Working Group Document States"
#   RFC 6175 "Requirements to Extend the Datatracker for IETF Working Group Chairs and Authors"
#   RFC 6292 "Requirements for a Working Group Charter Tool"
#   RFC 6293 "Requirements for Internet-Draft Tracking by the IETF Community in the Datatracker"
#   RFC 6322 "Datatracker States and Annotations for the IAB, IRTF, and Independent Submission Streams"
#   RFC 6359 "Datatracker Extensions to Include IANA and RFC Editor Processing Information"
#   RFC 7760 "Statement of Work for Extensions to the IETF Datatracker for Author Statistics"

import dateutil.tz
import glob
import json
import logging
import os
import re
import requests
import requests_cache
import sys
import time
import urllib.parse

from datetime          import date, datetime, timedelta, timezone
from typing            import List, Optional, Tuple, Dict, Iterator, Type, TypeVar, Any, Generic
from typing_extensions import Self
from dataclasses       import dataclass, field
from pathlib           import Path
from pydantic          import ValidationError 

# =================================================================================================================================
# Import the types representing the datatracker API endpoints:

from ietfdata.dtbackend         import *
from ietfdata.datatracker_types import *

# =================================================================================================================================
# A class to represent the datatracker:

@dataclass
class Hints(Generic[T]):
    obj_type :  Type[T]
    sort_by : str


class DataTracker:
    """
    A class for interacting with the IETF Datatracker.

    There are two ways to instantiate this class, depending on how it is to
    be used. If the intention is to perform live queries of the datatracker,
    for example as part of a tool that provides an interactive dashboard or
    status report, then it should be instatiated using the DTBackendLive
    backend, as follows:

        dt = DataTracker(DTBackendLive())

    In this case, the DataTracker class will directly query the online IETF
    datatracker for every request you make.

    Alternatively, if the intent is to perform analysis of a snapshot of the
    data, for example if writing a research paper, a dissertation, or as part
    of a student project, then the DTBackendArchive should be used:

        dt = DataTracker(DTBackendArchive(sqlite_file="ietfdata.sqlite"))

    In this case, the DataTracker class will create the specified sqlite file
    if it doesn't exist and download a complete copy of the data from the IETF
    datatracker (this will take around 24 hours, and will generate an sqlite
    file that is around 1.5GB in size; if the download is interrupted, it is
    safe to rerun the above operation and the download should resume where it
    left-off). Once the sqlite file is downloaded, future instantiations of the
    DataTracker will read from it directly and will not access the online IETF
    datatracker, making them much faster and avoiding overloading the IETF's
    servers.

    If you are working on a paper, project, or dissertation with a group of
    people, one person should create the ietfdata.sqlite file, then share a
    copy with the others. This avoids overloading the IETF's servers, and
    ensures that everyone working in the group generates the same results.

    If you are using the MailArchive3 class to work with the IETF email
    archive, it is safe to use the same sqlite file for the mail archive
    and the datatracker.
    """
    backend : DTBackend

    def __init__(self, backend : DTBackend):
        logging.getLogger('requests').setLevel('ERROR')
        logging.getLogger('requests_cache').setLevel('ERROR')
        logging.getLogger("urllib3").setLevel('ERROR')
        logging.basicConfig(level=os.getenv("IETFDATA_LOGLEVEL", default="INFO"))
        self.log = logging.getLogger("ietfdata")

        self.backend = backend
        self.backend.update()

        self._hints = {} # type: Dict[str, Hints]
        self._hints["/api/v1/doc/ballotdocevent/"]                 = Hints(BallotDocumentEvent,         "id")
        self._hints["/api/v1/doc/ballottype/"]                     = Hints(BallotType,                  "slug")
        self._hints["/api/v1/doc/docevent/"]                       = Hints(DocumentEvent,               "id")
        self._hints["/api/v1/doc/document/"]                       = Hints(Document,                    "id")
        self._hints["/api/v1/doc/documentauthor/"]                 = Hints(DocumentAuthor,              "id")
        self._hints["/api/v1/doc/documenturl/"]                    = Hints(DocumentUrl,                 "id")
        self._hints["/api/v1/doc/relateddocument/"]                = Hints(RelatedDocument,             "id")
        self._hints["/api/v1/doc/state/"]                          = Hints(DocumentState,               "id")
        self._hints["/api/v1/doc/statetype/"]                      = Hints(DocumentStateType,           "slug")
        self._hints["/api/v1/group/changestategroupevent/"]        = Hints(GroupStateChangeEvent,       "id")
        self._hints["/api/v1/group/group/"]                        = Hints(Group,                       "id")
        self._hints["/api/v1/group/groupevent/"]                   = Hints(GroupEvent,                  "id")
        self._hints["/api/v1/group/grouphistory/"]                 = Hints(GroupHistory,                "id")
        self._hints["/api/v1/group/groupmilestone/"]               = Hints(GroupMilestone,              "id")
        self._hints["/api/v1/group/groupmilestonehistory/"]        = Hints(GroupMilestoneHistory,       "id")
        self._hints["/api/v1/group/groupurl/"]                     = Hints(GroupUrl,                    "id")
        self._hints["/api/v1/group/milestonegroupevent/"]          = Hints(GroupMilestoneEvent,         "id")
        self._hints["/api/v1/group/role/"]                         = Hints(GroupRole,                   "id")
        self._hints["/api/v1/group/rolehistory/"]                  = Hints(GroupRoleHistory,            "id")
        self._hints["/api/v1/ipr/genericiprdisclosure/"]           = Hints(GenericIPRDisclosure,        "id")
        self._hints["/api/v1/ipr/holderiprdisclosure/"]            = Hints(HolderIPRDisclosure,         "id")
        self._hints["/api/v1/ipr/iprdisclosurebase/"]              = Hints(IPRDisclosureBase,           "id")
        self._hints["/api/v1/ipr/thirdpartyiprdisclosure/"]        = Hints(ThirdPartyIPRDisclosure,     "id")
        self._hints["/api/v1/mailinglists/list/"]                  = Hints(EmailList,                   "id")
        self._hints["/api/v1/mailinglists/subscribed/"]            = Hints(EmailListSubscriptions,      "id")
        self._hints["/api/v1/meeting/attended/"]                   = Hints(MeetingAttended,             "id")
        self._hints["/api/v1/meeting/meeting/"]                    = Hints(Meeting,                     "id")
        self._hints["/api/v1/meeting/registration/"]               = Hints(MeetingRegistrationOld,      "id")
        self._hints["/api/v1/meeting/schedtimesessassignment/"]    = Hints(SessionAssignment,           "id")
        self._hints["/api/v1/meeting/schedule/"]                   = Hints(Schedule,                    "id")
        self._hints["/api/v1/meeting/schedulingevent/"]            = Hints(SchedulingEvent,             "id")
        self._hints["/api/v1/meeting/session/"]                    = Hints(Session,                     "id")
        self._hints["/api/v1/meeting/timeslot/"]                   = Hints(Timeslot,                    "id")
        self._hints["/api/v1/name/ballotpositionname/"]            = Hints(BallotPositionName,          "slug")
        self._hints["/api/v1/name/docrelationshipname/"]           = Hints(RelationshipType,            "slug")
        self._hints["/api/v1/name/doctagname/"]                    = Hints(DocumentTag,                 "slug")
        self._hints["/api/v1/name/doctypename/"]                   = Hints(DocumentType,                "slug")
        self._hints["/api/v1/name/extresourcename/"]               = Hints(ExtResourceName,             "slug")
        self._hints["/api/v1/name/extresourcetypename/"]           = Hints(ExtResourceTypeName,         "slug")
        self._hints["/api/v1/name/groupmilestonestatename/"]       = Hints(GroupMilestoneStateName,     "slug")
        self._hints["/api/v1/name/groupstatename/"]                = Hints(GroupState,                  "slug")
        self._hints["/api/v1/name/grouptypename/"]                 = Hints(GroupTypeName,               "slug")
        self._hints["/api/v1/name/meetingtypename/"]               = Hints(MeetingType,                 "slug")
        self._hints["/api/v1/name/iprdisclosurestatename/"]        = Hints(IPRDisclosureState,          "slug")
        self._hints["/api/v1/name/iprlicensetypename/"]            = Hints(IPRLicenseType,              "slug")
        self._hints["/api/v1/name/reviewassignmentstatename/"]     = Hints(ReviewAssignmentState,       "slug")
        self._hints["/api/v1/name/reviewresultname/"]              = Hints(ReviewResultType,            "slug")
        self._hints["/api/v1/name/reviewtypename/"]                = Hints(ReviewType,                  "slug")
        self._hints["/api/v1/name/reviewrequeststatename/"]        = Hints(ReviewRequestState,          "slug")
        self._hints["/api/v1/name/rolename/"]                      = Hints(RoleName,                    "slug")
        self._hints["/api/v1/name/sessionstatusname/"]             = Hints(SessionStatusName,           "slug")
        self._hints["/api/v1/name/sessionpurposename/"]            = Hints(SessionPurpose,              "slug")
        self._hints["/api/v1/name/streamname/"]                    = Hints(Stream,                      "slug")
        self._hints["/api/v1/person/alias/"]                       = Hints(PersonAlias,                 "id")
        self._hints["/api/v1/person/email/"]                       = Hints(Email,                       "address")
        self._hints["/api/v1/person/historicalemail/"]             = Hints(HistoricalEmail,             "address")
        self._hints["/api/v1/person/historicalperson/"]            = Hints(HistoricalPerson,            "id")
        self._hints["/api/v1/person/person/"]                      = Hints(Person,                      "id")
        self._hints["/api/v1/person/personevent/"]                 = Hints(PersonEvent,                 "id")
        self._hints["/api/v1/person/personextresource/"]           = Hints(PersonExtResource,           "id")
        self._hints["/api/v1/review/historicalreviewassignment/"]  = Hints(HistoricalReviewAssignment,  "id")
        self._hints["/api/v1/review/historicalreviewersettings/"]  = Hints(HistoricalReviewerSettings,  "id")
        self._hints["/api/v1/review/historicalreviewrequest/"]     = Hints(HistoricalReviewRequest,     "id")
        self._hints["/api/v1/review/historicalunavailableperiod/"] = Hints(HistoricalUnavailablePeriod, "id")
        self._hints["/api/v1/review/nextreviewerinteam/"]          = Hints(NextReviewerInTeam,          "id")
        self._hints["/api/v1/review/reviewassignment/"]            = Hints(ReviewAssignment,            "id")
        self._hints["/api/v1/review/reviewersettings/"]            = Hints(ReviewerSettings,            "id")
        self._hints["/api/v1/review/reviewrequest/"]               = Hints(ReviewRequest,               "id")
        self._hints["/api/v1/review/reviewsecretarysettings/"]     = Hints(ReviewSecretarySettings,     "id")
        self._hints["/api/v1/review/reviewteamsettings/"]          = Hints(ReviewTeamSettings,          "id")
        self._hints["/api/v1/review/reviewwish/"]                  = Hints(ReviewWish,                  "id")
        self._hints["/api/v1/review/unavailableperiod/"]           = Hints(UnavailablePeriod,           "id")
        self._hints["/api/v1/name/continentname/"]                 = Hints(Continent,                   "slug")
        self._hints["/api/v1/name/countryname/"]                   = Hints(Country,                     "slug")
        self._hints["/api/v1/stats/countryalias/"]                 = Hints(CountryAlias,                "id")
        self._hints["/api/v1/stats/meetingregistration/"]          = Hints(MeetingRegistration,         "id")
        self._hints["/api/v1/submit/submission/"]                  = Hints(Submission,                  "id")
        self._hints["/api/v1/submit/submissionevent/"]             = Hints(SubmissionEvent,             "id")


    # ----------------------------------------------------------------------------------------------------------------------------
    # Private methods to retrieve objects from the datatracker:

    def _retrieve(self, obj_uri: URI, obj_type: Type[T]) -> Optional[T]:
        self.log.debug(F"_retrieve {obj_uri}")
        obj_json = self.backend.datatracker_get_single(obj_uri)
        if obj_json is not None:
            #print(obj_json)
            #print(obj_type)
            res = None
            try:
                res = obj_type(**obj_json)
            except ValidationError as e:
                self.log.error(f"Cannot parse response {obj_json}: {e.errors()}")
            #try:
            #    res = self.pavlova.from_mapping(obj_json, obj_type)
            #except PavlovaParsingError:
            #    self.log.error(f"Cannot parse response {obj_json}")
            return res
        else:
            return None


    def _retrieve_multi(self, obj_uri: URI, obj_type: Type[T]) -> Iterator[T]:
        self.log.debug(F"_retrieve_multi: obj_uri {obj_uri}")
        obj_type_uri = type(obj_uri)(uri=obj_uri.uri)
        assert obj_uri.uri      is not None
        assert obj_type_uri.uri is not None
        obj_jsons = [] # type: List[Dict[str, Any]]
        for obj_json in self.backend.datatracker_get_multi(obj_uri):
            obj_jsons.append(obj_json)
        sort_by = self._hints[obj_type_uri.uri].sort_by
        for obj_json in sorted(obj_jsons, key=lambda k: k[sort_by]):
            #fetch_obj = self.pavlova.from_mapping(obj_json, obj_type) # type: T
            try:
                fetch_obj = obj_type(**obj_json)
                yield fetch_obj
            except ValidationError as e:
                self.log.error(f"Cannot parse response {obj_json}: {e.errors()}")


    # ----------------------------------------------------------------------------------------------------------------------------
    # Datatracker API endpoints returning information about people:
    # * https://datatracker.ietf.org/api/v1/person/person/
    # * https://datatracker.ietf.org/api/v1/person/person/20209/
    # * https://datatracker.ietf.org/api/v1/person/historicalperson/
    # * https://datatracker.ietf.org/api/v1/person/alias/

    def person(self, person_uri: PersonURI) -> Optional[Person]:
        return self._retrieve(person_uri, Person)


    def person_from_email(self, email_addr: str) -> Optional[Person]:
        email = self.email(EmailURI(uri=f"/api/v1/person/email/{email_addr}/"))
        if email is not None and email.person is not None:
            return self.person(email.person)
        else:
            return None


    def person_aliases(self,
            person        : Optional[Person] = None,
            name          : Optional[str] = None,
            name_contains : Optional[str] = None) -> Iterator[PersonAlias]:
        url = PersonAliasURI(uri="/api/v1/person/alias/")
        if person is not None:
            url.params["person"] = person.id
        if name is not None:
            url.params["name"] = name
        if name_contains is not None:
            url.params["name__contains"] = name_contains
        yield from self._retrieve_multi(url, PersonAlias)


    def person_history(self, person: Person) -> Iterator[HistoricalPerson]:
        url = HistoricalPersonURI(uri="/api/v1/person/historicalperson/")
        url.params["id"] = person.id
        yield from self._retrieve_multi(url, HistoricalPerson)


    def person_events(self, person: Person) -> Iterator[PersonEvent]:
        url = PersonEventURI(uri="/api/v1/person/personevent/")
        url.params["person"] = person.id
        yield from self._retrieve_multi(url, PersonEvent)


    def people(self,
            since : str ="1970-01-01T00:00:00",
            until : str ="2038-01-19T03:14:07",
            name                : Optional[str] = None,
            name_contains       : Optional[str] = None,
            name_ascii          : Optional[str] = None,
            name_ascii_contains : Optional[str] = None,
            name_plain          : Optional[str] = None,
            name_plain_contains : Optional[str] = None) -> Iterator[Person]:
        """
        A generator that returns people recorded in the datatracker. As of April
        2018, there are approximately 21500 people recorded.

        Parameters:
            since         -- Only return people with timestamp after this
            until         -- Only return people with timestamp before this
            name_contains -- Only return peopls whose name containing this string

        Returns:
            An iterator, where each element is as returned by the person() method
        """
        url = PersonURI(uri="/api/v1/person/person/")
        url.params["time__gte"] = since
        url.params["time__lt"]  = until
        if name is not None:
            url.params["name"] = name
        if name_contains is not None:
            url.params["name__contains"] = name_contains
        if name_ascii is not None:
            url.params["ascii"] = name_ascii
        if name_ascii_contains is not None:
            url.params["ascii__contains"] = name_ascii_contains
        if name_plain is not None:
            url.params["plain"] = name_plain
        if name_plain_contains is not None:
            url.params["plain__contains"] = name_plain_contains
        yield from self._retrieve_multi(url, Person)


    def person_ext_resource(self, person_ext_resource_uri: PersonExtResourceURI) -> Optional[PersonExtResource]:
        """
        Retrieve information about an external resource associated with a
        person.

        External resources include GitHub usernames and personal webpages,
        amongst other things.
        """
        return self._retrieve(person_ext_resource_uri, PersonExtResource)


    def person_ext_resources(self,
                             person        : Optional[Person] = None,
                             resource_name : Optional[ExtResourceName] = None,
                             resource_slug : Optional[str] = None) -> Iterator[PersonExtResource]:
        url = PersonExtResourceURI(uri="/api/v1/person/personextresource/")
        if person is not None:
            url.params["person"] = person.id
        if resource_name is not None:
            url.params["name"] = resource_name.slug
        if resource_slug is not None:
            url.params["name"] = resource_slug
        yield from self._retrieve_multi(url, PersonExtResource)


    def ext_resource_name(self, ext_resource_name_uri: ExtResourceNameURI) -> Optional[ExtResourceName]:
        return self._retrieve(ext_resource_name_uri, ExtResourceName)


    def ext_resource_name_from_slug(self, slug: str) -> Optional[ExtResourceName]:
        return self._retrieve(ExtResourceNameURI(uri=f"/api/v1/name/extresourcename/{slug}/"), ExtResourceName)


    def ext_resource_names(self) -> Iterator[ExtResourceName]:
        yield from self._retrieve_multi(ExtResourceNameURI(uri="/api/v1/name/extresourcename/"), ExtResourceName)


    def ext_resource_type_name(self, ext_resource_type_name_uri: ExtResourceTypeNameURI) -> Optional[ExtResourceTypeName]:
        return self._retrieve(ext_resource_type_name_uri, ExtResourceTypeName)


    def ext_resource_type_name_from_slug(self, slug: str) -> Optional[ExtResourceTypeName]:
        return self._retrieve(ExtResourceTypeNameURI(uri=f"/api/v1/name/extresourcetypename/{slug}/"), ExtResourceTypeName)


    def ext_resource_type_names(self) -> Iterator[ExtResourceTypeName]:
        yield from self._retrieve_multi(ExtResourceTypeNameURI(uri="/api/v1/name/extresourcetypename/"), ExtResourceTypeName)


    # ----------------------------------------------------------------------------------------------------------------------------
    # Datatracker API endpoints returning information about email addresses:
    # * https://datatracker.ietf.org/api/v1/person/email/csp@csperkins.org/
    # * https://datatracker.ietf.org/api/v1/person/historicalemail/

    def email(self, email_uri: EmailURI) -> Optional[Email]:
        return self._retrieve(email_uri, Email)


    def email_for_address(self, email_addr: str) -> Optional[Email]:
        uri = EmailURI(uri=f"/api/v1/person/email/{email_addr}/")
        return self.email(uri)


    def email_for_person(self, person: Person) -> Iterator[Email]:
        uri = EmailURI(uri="/api/v1/person/email/")
        uri.params["person"] = person.id
        yield from self._retrieve_multi(uri, Email)


    def email_history_for_address(self, email_addr: str) -> Iterator[HistoricalEmail]:
        uri = HistoricalEmailURI(uri="/api/v1/person/historicalemail/")
        uri.params["address"] = email_addr
        yield from self._retrieve_multi(uri, HistoricalEmail)


    def email_history_for_person(self, person: Person) -> Iterator[HistoricalEmail]:
        uri = HistoricalEmailURI(uri="/api/v1/person/historicalemail/")
        uri.params["person"] = person.id
        yield from self._retrieve_multi(uri, HistoricalEmail)


    def emails(self,
               since : str ="1970-01-01T00:00:00",
               until : str ="2038-01-19T03:14:07",
               addr_contains : Optional[str] = None) -> Iterator[Email]:
        """
        A generator that returns email addresses recorded in the datatracker.

        Parameters:
            since         -- Only return email addresses with timestamp after this
            until         -- Only return email addresses with timestamp before this
            addr_contains -- Only return email addresses containing this substring

        Returns:
            An iterator, where each element is an Email object
        """
        url = EmailURI(uri="/api/v1/person/email/")
        url.params["time__gte"] = since
        url.params["time__lt"]   = until
        if addr_contains is not None:
            url.params["address__contains"] = addr_contains
        yield from self._retrieve_multi(url, Email)


    # ----------------------------------------------------------------------------------------------------------------------------
    # Datatracker API endpoints returning information about documents:
    # * https://datatracker.ietf.org/api/v1/doc/document/                        - list of documents
    # * https://datatracker.ietf.org/api/v1/doc/document/draft-ietf-avt-rtp-new/ - info about document

    def document(self, document_uri: DocumentURI) -> Optional[Document]:
        return self._retrieve(document_uri, Document)


    # WARNING: the `since` and `until` parameters refer to the dates when the document metadata
    # was last modified, not the dates when the document was last updated. Use `submissions()`
    # with `date_since` and `date_until` to find documents updated in a particular time window.
    def documents(self,
            since   : str = "1970-01-01T00:00:00",
            until   : str = "2038-01-19T03:14:07",
            doctype : Optional[DocumentType] = None,
            state   : Optional[DocumentState] = None,
            stream  : Optional[Stream]       = None,
            group   : Optional[Group]        = None) -> Iterator[Document]:
        url = DocumentURI(uri="/api/v1/doc/document/")
        url.params["time__gte"] = since
        url.params["time__lt"] = until
        if doctype is not None:
            url.params["type"] = doctype.slug
        if state is not None:
            url.params["states"] = state.id
        if stream is not None:
            url.params["stream"] = stream.slug
        if group is not None:
            url.params["group"] = group.id
        yield from self._retrieve_multi(url, Document)


    # Datatracker API endpoints returning information about document aliases:

    def document_from_draft(self, draft: str) -> Optional[Document]:
        """
        Returns the document with the specified name.

        Parameters:
            name -- The name of the document to lookup (e.g, "draft-ietf-avt-rtp-new")

        Returns:
            A Document object
        """
        assert draft.startswith("draft-")
        assert not "," in draft
        return self.document(DocumentURI(uri="/api/v1/doc/document/" + draft + "/"))


    def document_from_rfc(self, rfc: str) -> Optional[Document]:
        """
        Returns the document that became the specified RFC.

        Parameters:
            rfc -- The RFC to lookup (e.g., "rfc3550" or "RFC3550")

        Returns:
            A Document object
        """
        assert rfc.lower().startswith("rfc")
        return self.document(DocumentURI(uri="/api/v1/doc/document/" + rfc.lower() + "/"))


    def documents_from_bcp(self, bcp: str) -> Iterator[Document]:
        """
        Returns the document that became the specified BCP.

        Parameters:
            bcp -- The BCP to lookup (e.g., "bcp205" or "BCP205")

        Returns:
            A list of Document objects
        """
        assert bcp.lower().startswith("bcp")
        bcp_doc = self.document(DocumentURI(uri="/api/v1/doc/document/" + bcp + "/"))
        for rel_doc in self.related_documents(source = bcp_doc, relationship_type_slug = "contains"):
            rfc = self.document(rel_doc.target)
            assert rfc is not None
            yield rfc


    def documents_from_std(self, std: str) -> Iterator[Document]:
        """
        Returns the document that became the specified STD.

        Parameters:
            std -- The STD to lookup (e.g., "std68" or "STD68")

        Returns:
            A list of Document objects
        """
        assert std.lower().startswith("std")
        std_doc = self.document(DocumentURI(uri="/api/v1/doc/document/" + std + "/"))
        for rel_doc in self.related_documents(source = std_doc, relationship_type_slug = "contains"):
            rfc = self.document(rel_doc.target)
            assert rfc is not None
            yield rfc


    # Datatracker API endpoints returning information about document types:
    # * https://datatracker.ietf.org/api/v1/name/doctypename/

    def document_type(self, doc_type_uri: DocumentTypeURI) -> Optional[DocumentType]:
        return self._retrieve(doc_type_uri, DocumentType)


    def document_type_from_slug(self, slug: str) -> Optional[DocumentType]:
        return self._retrieve(DocumentTypeURI(uri=f"/api/v1/name/doctypename/{slug}/"), DocumentType)


    def document_types(self) -> Iterator[DocumentType]:
        yield from self._retrieve_multi(DocumentTypeURI(uri="/api/v1/name/doctypename/"), DocumentType)


    # Datatracker API endpoints returning information about document states:
    # * https://datatracker.ietf.org/api/v1/doc/state/                           - Types of state a document can be in
    # * https://datatracker.ietf.org/api/v1/doc/statetype/                       - Possible types of state for a document

    def document_state(self, state_uri: DocumentStateURI) -> Optional[DocumentState]:
        return self._retrieve(state_uri, DocumentState)

    def document_state_from_slug(self, state_type: DocumentStateType, slug: str) -> DocumentState:
        states = list(self.document_states(state_type, slug))
        assert len(states) == 1
        return states[0]

    def document_states(self,
            state_type : Optional[DocumentStateType] = None,
            slug       : Optional[str]               = None) -> Iterator[DocumentState]:
        url = DocumentStateURI(uri="/api/v1/doc/state/")
        if state_type is not None:
            url.params["type"] = state_type.slug
        if slug is not None:
            url.params["slug"] = slug
        yield from self._retrieve_multi(url, DocumentState)


    def document_state_type(self, state_type_uri : DocumentStateTypeURI) -> Optional[DocumentStateType]:
        return self._retrieve(state_type_uri, DocumentStateType)


    def document_state_type_from_slug(self, slug: str) -> Optional[DocumentStateType]:
        return self._retrieve(DocumentStateTypeURI(uri=f"/api/v1/doc/statetype/{slug}/"), DocumentStateType)


    def document_state_types(self) -> Iterator[DocumentStateType]:
        url = DocumentStateTypeURI(uri="/api/v1/doc/statetype/")
        yield from self._retrieve_multi(url, DocumentStateType)


    # Datatracker API endpoints returning information about document events:
    # * https://datatracker.ietf.org/api/v1/doc/docevent/                        - list of document events
    # * https://datatracker.ietf.org/api/v1/doc/docevent/?doc=...                - events for a document
    # * https://datatracker.ietf.org/api/v1/doc/docevent/?by=...                 - events by a person (as /api/v1/person/person)
    # * https://datatracker.ietf.org/api/v1/doc/docevent/?time=...               - events by time
    #   https://datatracker.ietf.org/api/v1/doc/statedocevent/                   - subset of /api/v1/doc/docevent/; same parameters
    #   https://datatracker.ietf.org/api/v1/doc/newrevisiondocevent/             -               "                "
    #   https://datatracker.ietf.org/api/v1/doc/submissiondocevent/              -               "                "
    #   https://datatracker.ietf.org/api/v1/doc/writeupdocevent/                 -               "                "
    #   https://datatracker.ietf.org/api/v1/doc/consensusdocevent/               -               "                "
    #   https://datatracker.ietf.org/api/v1/doc/reviewrequestdocevent/           -               "                "
    #   https://datatracker.ietf.org/api/v1/doc/lastcalldocevent/                -               "                "
    #   https://datatracker.ietf.org/api/v1/doc/telechatdocevent/                -               "                "
    #   https://datatracker.ietf.org/api/v1/doc/initialreviewdocevent/           -               "                "
    #   https://datatracker.ietf.org/api/v1/doc/editedauthorsdocevent/           -               "                "

    def document_event(self, event_uri : DocumentEventURI) -> Optional[DocumentEvent]:
        return self._retrieve(event_uri, DocumentEvent)


    def document_events(self,
                        since      : str = "1970-01-01T00:00:00",
                        until      : str = "2038-01-19T03:14:07",
                        doc        : Optional[Document] = None,
                        by         : Optional[Person]   = None,
                        event_type : Optional[str]      = None) -> Iterator[DocumentEvent]:
        """
        A generator returning information about document events.

        Parameters:
            since      -- Only return document events with timestamp after this
            until      -- Only return document events with timestamp after this
            doc        -- Only return document events for this document
            by         -- Only return document events by this person
            event_type -- Only return document events with this type

        Returns:
           A sequence of DocumentEvent objects
        """
        url = DocumentEventURI(uri="/api/v1/doc/docevent/")
        url.params["time__gte"] = since
        url.params["time__lt"] = until
        if doc is not None:
            url.params["doc"]  = doc.id
            url.params_alt["doc"] = doc.name
        if by is not None:
            url.params["by"]   = by.id
        if event_type is not None:
            url.params["type"] = event_type
        yield from self._retrieve_multi(url, DocumentEvent)


    # Datatracker API endpoints returning information about document authorship:
    # * https://datatracker.ietf.org/api/v1/doc/documentauthor/?document=...     - authors of a document
    # * https://datatracker.ietf.org/api/v1/doc/documentauthor/?person=...       - documents by person
    # * https://datatracker.ietf.org/api/v1/doc/documentauthor/?email=...        - documents by person

    def document_authors(self, document : Document) -> Iterator[DocumentAuthor]:
        url = DocumentAuthorURI(uri="/api/v1/doc/documentauthor/")
        url.params["document"] = document.id
        yield from self._retrieve_multi(url, DocumentAuthor)


    def documents_authored_by_person(self, person : Person) -> Iterator[DocumentAuthor]:
        url = DocumentAuthorURI(uri="/api/v1/doc/documentauthor/")
        url.params["person"] = person.id
        yield from self._retrieve_multi(url, DocumentAuthor)


    def documents_authored_by_email(self, email : Email) -> Iterator[DocumentAuthor]:
        url = DocumentAuthorURI(uri="/api/v1/doc/documentauthor/")
        url.params["email"] = email.address
        yield from self._retrieve_multi(url, DocumentAuthor)


    # Datatracker API endpoints returning information about related documents:
    #   https://datatracker.ietf.org/api/v1/doc/relateddocument/?source=...      - documents that source draft relates to
    #   https://datatracker.ietf.org/api/v1/doc/relateddocument/?target=...      - documents that relate to target draft
    #   https://datatracker.ietf.org/api/v1/doc/relateddochistory/

    def related_documents(self,
                          source                 : Optional[Document]         = None,
                          target                 : Optional[Document]         = None,
                          relationship_type      : Optional[RelationshipType] = None,
                          relationship_type_slug : Optional[str] = None) -> Iterator[RelatedDocument]:

        url = RelatedDocumentURI(uri="/api/v1/doc/relateddocument/")
        if source is not None:
            url.params["source"] = source.id
            url.params_alt["source"] = source.name
        if target is not None:
            url.params["target"] = target.id
            url.params_alt["target"] = target.name
        if relationship_type is not None:
            url.params["relationship"] = relationship_type.slug
        if relationship_type_slug is not None:
            url.params["relationship"] = relationship_type_slug
        yield from self._retrieve_multi(url, RelatedDocument)


    def relationship_type(self, relationship_type_uri: RelationshipTypeURI) -> Optional[RelationshipType]:
        """
        Retrieve a relationship type

        Parameters:
            relationship_type_uri -- The relationship type uri,
            as found in the resource_uri of a relationship type.

        Returns:
            A RelationshipType object
        """
        return self._retrieve(relationship_type_uri, RelationshipType)


    def relationship_type_from_slug(self, slug: str) -> Optional[RelationshipType]:
        return self._retrieve(RelationshipTypeURI(uri=f"/api/v1/name/docrelationshipname/{slug}/"), RelationshipType)


    def relationship_types(self) -> Iterator[RelationshipType]:
        """
        A generator returning the possible relationship types

        Parameters:
           None

        Returns:
            An iterator of RelationshipType objects
        """
        url = RelationshipTypeURI(uri="/api/v1/name/docrelationshipname/")
        yield from self._retrieve_multi(url, RelationshipType)


    # Datatracker API endpoints returning information about document history:
    #   https://datatracker.ietf.org/api/v1/doc/dochistory/
    #   https://datatracker.ietf.org/api/v1/doc/dochistoryauthor/

    # FIXME: implement document history methods


    # ----------------------------------------------------------------------------------------------------------------------------
    # Datatracker API endpoints returning information about ballots and document approval:
    # * https://datatracker.ietf.org/api/v1/name/ballotpositionname/
    #   https://datatracker.ietf.org/api/v1/doc/ballotpositiondocevent/
    # * https://datatracker.ietf.org/api/v1/doc/ballottype/
    # * https://datatracker.ietf.org/api/v1/doc/ballotdocevent/

    def ballot_position_name(self, ballot_position_name_uri : BallotPositionNameURI) -> Optional[BallotPositionName]:
        return self._retrieve(ballot_position_name_uri, BallotPositionName)


    def ballot_position_name_from_slug(self, slug: str) -> Optional[BallotPositionName]:
        return self._retrieve(BallotPositionNameURI(uri=f"/api/v1/name/ballotpositionname/{slug}/"), BallotPositionName)


    def ballot_position_names(self) -> Iterator[BallotPositionName]:
        """
        A generator returning information about ballot position names. These describe
        the names of the responses that a person can give to a ballot (e.g., "Discuss",
        "Abstain", "No Objection", ...).

        Returns:
           A sequence of BallotPositionName objects
        """
        url = BallotPositionNameURI(uri="/api/v1/name/ballotpositionname/")
        yield from self._retrieve_multi(url, BallotPositionName)


    def ballot_type(self, ballot_type_uri : BallotTypeURI) -> Optional[BallotType]:
        return self._retrieve(ballot_type_uri, BallotType)


    def ballot_types(self, doc_type : Optional[DocumentType]) -> Iterator[BallotType]:
        """
        A generator returning information about ballot types.

        Parameters:
            doc_type     -- Only return ballot types relating to this document type

        Returns:
           A sequence of BallotType objects
        """
        url = BallotTypeURI(uri="/api/v1/doc/ballottype/")
        if doc_type is not None:
            url.params["doc_type"] = doc_type.slug
        yield from self._retrieve_multi(url, BallotType)



    def ballot_document_event(self, ballot_event_uri : BallotDocumentEventURI) -> Optional[BallotDocumentEvent]:
        return self._retrieve(ballot_event_uri, BallotDocumentEvent)


    def ballot_document_events(self,
                        since       : str = "1970-01-01T00:00:00",
                        until       : str = "2038-01-19T03:14:07",
                        ballot_type : Optional[BallotType]    = None,
                        event_type  : Optional[str]           = None,
                        by          : Optional[Person]        = None,
                        doc         : Optional[Document]      = None) -> Iterator[BallotDocumentEvent]:
        """
        A generator returning information about ballot document events.

        Parameters:
            since        -- Only return ballot document events with timestamp after this
            until        -- Only return ballot document events with timestamp after this
            ballot_type  -- Only return ballot document events of this ballot type
            event_type   -- Only return ballot document events with this type
            by           -- Only return ballot document events by this person
            doc          -- Only return ballot document events that relate to this document

        Returns:
           A sequence of BallotDocumentEvent objects
        """
        url = BallotDocumentEventURI(uri="/api/v1/doc/ballotdocevent/")
        url.params["time__gte"] = since
        url.params["time__lt"] = until
        if ballot_type is not None:
            url.params["ballot_type"] = ballot_type.id
        if by is not None:
            url.params["by"] = by.id
        if doc is not None:
            url.params["doc"] = doc.id
            url.params_alt["doc"] = doc.name
        if event_type is not None:
            url.params["type"] = event_type
        yield from self._retrieve_multi(url, BallotDocumentEvent)


    # ----------------------------------------------------------------------------------------------------------------------------
    # Datatracker API endpoints returning information about document submissions:
    # * https://datatracker.ietf.org/api/v1/submit/submission/
    # * https://datatracker.ietf.org/api/v1/submit/submissionevent/
    #   https://datatracker.ietf.org/api/v1/submit/submissioncheck/
    #   https://datatracker.ietf.org/api/v1/submit/preapproval/

    def submission(self, submission_uri: SubmissionURI) -> Optional[Submission]:
        return self._retrieve(submission_uri, Submission)


    def submissions(self,
            date_since           : str = "1970-01-01",
            date_until           : str = "2038-01-19") -> Iterator[Submission]:
        url = SubmissionURI(uri="/api/v1/submit/submission/")
        url.params["submission_date__gte"] = date_since
        url.params["submission_date__lt"] = date_until
        yield from self._retrieve_multi(url, Submission)


    def submission_event(self, event_uri: SubmissionEventURI) -> Optional[SubmissionEvent]:
        return self._retrieve(event_uri, SubmissionEvent)


    def submission_events(self,
                        since      : str = "1970-01-01T00:00:00",
                        until      : str = "2038-01-19T03:14:07",
                        by         : Optional[Person]     = None,
                        submission : Optional[Submission] = None) -> Iterator[SubmissionEvent]:
        """
        A generator returning information about submission events.

        Parameters:
            since      -- Only return submission events with timestamp after this
            until      -- Only return submission events with timestamp after this
            by         -- Only return submission events by this person
            submission -- Only return submission events about this submission

        Returns:
           A sequence of SubmissionEvent objects
        """
        url = SubmissionEventURI(uri="/api/v1/submit/submissionevent/")
        url.params["time__gte"] = since
        url.params["time__lt"] = until
        if by is not None:
            url.params["by"] = by.id
        if submission is not None:
            url.params["submission"] = submission.id
        yield from self._retrieve_multi(url, SubmissionEvent)

    # ----------------------------------------------------------------------------------------------------------------------------
    # Datatracker API endpoints returning miscellaneous information about documents:
    #   https://datatracker.ietf.org/api/v1/doc/docreminder/
    #   https://datatracker.ietf.org/api/v1/doc/deletedevent/

    # FIXME: implement these

    #   https://datatracker.ietf.org/api/v1/doc/documenturl/
    def document_url(self, document_url_uri: DocumentUrlURI) -> Optional[DocumentUrl]:
        return self._retrieve(document_url_uri, DocumentUrl)
    
    
    def document_urls(self, doc: Optional[Document] = None) -> Iterator[DocumentUrl]:
        url = DocumentUrlURI(uri="/api/v1/doc/documenturl/")
        if doc is not None:
            url.params["doc"] = doc.id
            url.params_alt["doc"] = doc.name
        yield from self._retrieve_multi(url, DocumentUrl)


    #   https://datatracker.ietf.org/api/v1/name/doctagname/
    def document_tag(self, tag_uri: DocumentTagURI) -> Optional[DocumentTag]:
        return self._retrieve(tag_uri, DocumentTag)


    # ----------------------------------------------------------------------------------------------------------------------------
    # Datatracker API endpoints returning information about RFC publication streams:
    # * https://datatracker.ietf.org/api/v1/name/streamname/

    def stream(self, stream_uri: StreamURI) -> Optional[Stream]:
        return self._retrieve(stream_uri, Stream)


    def stream_from_slug(self, slug: str) -> Optional[Stream]:
        return self._retrieve(StreamURI(uri=f"/api/v1/name/streamname/{slug}/"), Stream)


    def streams(self) -> Iterator[Stream]:
        yield from self._retrieve_multi(StreamURI(uri="/api/v1/name/streamname/"), Stream)


    # ----------------------------------------------------------------------------------------------------------------------------
    # Datatracker API endpoints returning information about working groups:
    # * https://datatracker.ietf.org/api/v1/group/group/                               - list of groups
    # * https://datatracker.ietf.org/api/v1/group/group/2161/                          - info about group 2161
    # * https://datatracker.ietf.org/api/v1/group/grouphistory/?group=2161             - history
    # * https://datatracker.ietf.org/api/v1/group/groupurl/?group=2161                 - URLs
    # * https://datatracker.ietf.org/api/v1/group/groupevent/?group=2161               - events
    # * https://datatracker.ietf.org/api/v1/group/groupmilestone/?group=2161           - Current milestones
    # * https://datatracker.ietf.org/api/v1/group/groupmilestonehistory/?group=2161    - Previous milestones
    # * https://datatracker.ietf.org/api/v1/group/milestonegroupevent/?group=2161      - changed milestones
    # * https://datatracker.ietf.org/api/v1/group/role/?group=2161                     - The current WG chairs and ADs of a group
    # * https://datatracker.ietf.org/api/v1/group/role/?person=20209                   - Groups a person is currently involved with
    # * https://datatracker.ietf.org/api/v1/group/role/?email=csp@csperkins.org        - Groups a person is currently involved with
    # * https://datatracker.ietf.org/api/v1/group/rolehistory/?group=2161              - The previous WG chairs and ADs of a group
    # * https://datatracker.ietf.org/api/v1/group/rolehistory/?person=20209            - Groups person was previously involved with
    # * https://datatracker.ietf.org/api/v1/group/rolehistory/?email=csp@csperkins.org - Groups person was previously involved with
    # * https://datatracker.ietf.org/api/v1/group/changestategroupevent/?group=2161    - Group state changes
    #   https://datatracker.ietf.org/api/v1/group/groupstatetransitions                - ???
    # * https://datatracker.ietf.org/api/v1/name/groupstatename/
    # * https://datatracker.ietf.org/api/v1/name/grouptypename/

    def group(self, group_uri: GroupURI) -> Optional[Group]:
        return self._retrieve(group_uri, Group)


    def group_from_acronym(self, acronym: str) -> Optional[Group]:
        url = GroupURI(uri="/api/v1/group/group/")
        url.params["acronym"] = acronym
        groups = list(self._retrieve_multi(url, Group))
        if len(groups) == 0:
            return None
        elif len(groups) == 1:
            return groups[0]
        else:
            raise RuntimeError("group_from_acronym: multiple groups returned, expected 0 or 1")


    def groups(self,
            since         : str                  = "1970-01-01T00:00:00",
            until         : str                  = "2038-01-19T03:14:07",
            name_contains : Optional[str]        = None,
            state         : Optional[GroupState] = None,
            parent        : Optional[Group]      = None) -> Iterator[Group]:
        url = GroupURI(uri="/api/v1/group/group/")
        url.params["time__gte"]       = since
        url.params["time__lt"]       = until
        if name_contains is not None:
            url.params["name__contains"] = name_contains
        if state is not None:
            url.params["state"] = state.slug
        if parent is not None:
            url.params["parent"] = parent.id
        yield from self._retrieve_multi(url, Group)


    def group_history(self, group_history_uri: GroupHistoryURI) -> Optional[GroupHistory]:
        return self._retrieve(group_history_uri, GroupHistory)


    def group_histories_from_acronym(self, acronym: str) -> Iterator[GroupHistory]:
        url = GroupHistoryURI(uri="/api/v1/group/grouphistory/")
        url.params["acronym"] = acronym
        yield from self._retrieve_multi(url, GroupHistory)


    def group_histories(self,
            since         : str                  = "1970-01-01T00:00:00",
            until         : str                  = "2038-01-19T03:14:07",
            group         : Optional[Group]      = None,
            state         : Optional[GroupState] = None,
            parent        : Optional[Group]      = None) -> Iterator[GroupHistory]:
        url = GroupHistoryURI(uri="/api/v1/group/grouphistory/")
        url.params["time__gte"]  = since
        url.params["time__lt"]  = until
        if group is not None:
            url.params["group"] = group.id
        if state is not None:
            url.params["state"] = state.slug
        if parent is not None:
            url.params["parent"] = parent.id
        yield from self._retrieve_multi(url, GroupHistory)


    def group_event(self, group_event_uri : GroupEventURI) -> Optional[GroupEvent]:
        return self._retrieve(group_event_uri, GroupEvent)


    def group_events(self,
            since         : str                  = "1970-01-01T00:00:00",
            until         : str                  = "2038-01-19T03:14:07",
            by            : Optional[Person]     = None,
            group         : Optional[Group]      = None,
            type          : Optional[str]        = None) -> Iterator[GroupEvent]:
        url = GroupEventURI(uri="/api/v1/group/groupevent/")
        url.params["time__gte"] = since
        url.params["time__lt"]  = until
        if by is not None:
            url.params["by"] = by.id
        if group is not None:
            url.params["group"] = group.id
        if type is not None:
            url.params["type"]  = type
        yield from self._retrieve_multi(url, GroupEvent)


    def group_url(self, group_url_uri: GroupUrlURI) -> Optional[GroupUrl]:
        return self._retrieve(group_url_uri, GroupUrl)


    def group_urls(self, group: Optional[Group] = None) -> Iterator[GroupUrl]:
        url = GroupUrlURI(uri="/api/v1/group/groupurl/")
        if group is not None:
            url.params["group"] = group.id
        yield from self._retrieve_multi(url, GroupUrl)


    def group_milestone_statename(self, group_milestone_statename_uri: GroupMilestoneStateNameURI) -> Optional[GroupMilestoneStateName]:
        return self._retrieve(group_milestone_statename_uri, GroupMilestoneStateName)


    def group_milestone_statenames(self) -> Iterator[GroupMilestoneStateName]:
        yield from self._retrieve_multi(GroupMilestoneStateNameURI(uri="/api/v1/name/groupmilestonestatename/"), GroupMilestoneStateName)


    def group_milestone(self, group_milestone_uri : GroupMilestoneURI) -> Optional[GroupMilestone]:
        return self._retrieve(group_milestone_uri, GroupMilestone)


    def group_milestones(self,
            since         : str                               = "1970-01-01T00:00:00",
            until         : str                               = "2038-01-19T03:14:07",
            group         : Optional[Group]                   = None,
            state         : Optional[GroupMilestoneStateName] = None) -> Iterator[GroupMilestone]:
        url = GroupMilestoneURI(uri="/api/v1/group/groupmilestone/")
        url.params["time__gte"]       = since
        url.params["time__lt"]       = until
        if group is not None:
            url.params["group"] = group.id
        if state is not None:
            url.params["state"] = state.slug
        yield from self._retrieve_multi(url, GroupMilestone)


    def role_name(self, role_name_uri: RoleNameURI) -> Optional[RoleName]:
        return self._retrieve(role_name_uri, RoleName)


    def role_name_from_slug(self, slug: str) -> Optional[RoleName]:
        return self._retrieve(RoleNameURI(uri=f"/api/v1/name/rolename/{slug}/"), RoleName)


    def role_names(self) -> Iterator[RoleName]:
        yield from self._retrieve_multi(RoleNameURI(uri="/api/v1/name/rolename/"), RoleName)


    def group_role(self, group_role_uri : GroupRoleURI) -> Optional[GroupRole]:
        return self._retrieve(group_role_uri, GroupRole)


    def group_roles(self,
            email         : Optional[str]           = None,
            group         : Optional[Group]         = None,
            name          : Optional[RoleName]      = None,
            person        : Optional[Person]        = None) -> Iterator[GroupRole]:
        url = GroupRoleURI(uri="/api/v1/group/role/")
        if email is not None:
            url.params["email"] = email
        if group is not None:
            url.params["group"] = group.id
        if name is not None:
            url.params["name"] = name.slug
        if person is not None:
            url.params["person"] = person.id
        yield from self._retrieve_multi(url, GroupRole)


    def group_role_history(self, group_role_history_uri : GroupRoleHistoryURI) -> Optional[GroupRoleHistory]:
        return self._retrieve(group_role_history_uri, GroupRoleHistory)


    def group_role_histories(self,
            email         : Optional[str]           = None,
            group         : Optional[GroupHistory]  = None,
            name          : Optional[RoleName]      = None,
            person        : Optional[Person]        = None) -> Iterator[GroupRoleHistory]:
        url = GroupRoleHistoryURI(uri="/api/v1/group/rolehistory/")
        if email is not None:
            url.params["email"] = email
        if group is not None:
            url.params["group"] = group.id
        if name is not None:
            url.params["name"] = name.slug
        if person is not None:
            url.params["person"] = person.id
        yield from self._retrieve_multi(url, GroupRoleHistory)


    def group_milestone_history(self, group_milestone_history_uri : GroupMilestoneHistoryURI) -> Optional[GroupMilestoneHistory]:
        return self._retrieve(group_milestone_history_uri, GroupMilestoneHistory)


    def group_milestone_histories(self,
            since         : str                               = "1970-01-01T00:00:00",
            until         : str                               = "2038-01-19T03:14:07",
            group         : Optional[Group]                   = None,
            milestone     : Optional[GroupMilestone]          = None,
            state         : Optional[GroupMilestoneStateName] = None) -> Iterator[GroupMilestoneHistory]:
        url = GroupMilestoneHistoryURI(uri="/api/v1/group/groupmilestonehistory/")
        url.params["time__gte"]       = since
        url.params["time__lt"]       = until
        if group is not None:
            url.params["group"] = group.id
        if milestone is not None:
            url.params["milestone"] = milestone.id
        if state is not None:
            url.params["state"] = state.slug
        yield from self._retrieve_multi(url, GroupMilestoneHistory)


    def group_milestone_event(self, group_milestone_event_uri : GroupMilestoneEventURI) -> Optional[GroupMilestoneEvent]:
        return self._retrieve(group_milestone_event_uri, GroupMilestoneEvent)


    def group_milestone_events(self,
            since         : str                        = "1970-01-01T00:00:00",
            until         : str                        = "2038-01-19T03:14:07",
            by            : Optional[Person]           = None,
            group         : Optional[Group]            = None,
            milestone     : Optional[GroupMilestone]   = None,
            type          : Optional[str]              = None) -> Iterator[GroupMilestoneEvent]:
        url = GroupMilestoneEventURI(uri="/api/v1/group/milestonegroupevent/")
        url.params["time__gte"] = since
        url.params["time__lt"]  = until
        if by is not None:
            url.params["by"] = by.id
        if group is not None:
            url.params["group"] = group.id
        if milestone is not None:
            url.params["milestone"] = milestone.id
        if type is not None:
            url.params["type"] = type
        yield from self._retrieve_multi(url, GroupMilestoneEvent)


    def group_state_change_event(self, group_state_change_event_uri : GroupStateChangeEventURI) -> Optional[GroupStateChangeEvent]:
        return self._retrieve(group_state_change_event_uri, GroupStateChangeEvent)


    def group_state_change_events(self,
            since         : str                        = "1970-01-01T00:00:00",
            until         : str                        = "2038-01-19T03:14:07",
            by            : Optional[Person]           = None,
            group         : Optional[Group]            = None,
            state         : Optional[GroupState]       = None) -> Iterator[GroupStateChangeEvent]:
        url = GroupStateChangeEventURI(uri="/api/v1/group/changestategroupevent/")
        url.params["time__gte"]       = since
        url.params["time__lt"]       = until
        if by is not None:
            url.params["by"] = by.id
        if group is not None:
            url.params["group"] = group.id
        if state is not None:
            url.params["state"] = state.slug
        yield from self._retrieve_multi(url, GroupStateChangeEvent)


    def group_state(self, group_state_uri : GroupStateURI) -> Optional[GroupState]:
        return self._retrieve(group_state_uri, GroupState)


    def group_state_from_slug(self, slug : str) -> Optional[GroupState]:
        return self._retrieve(GroupStateURI(uri=f"/api/v1/name/groupstatename/{slug}/"), GroupState)


    def group_states(self) -> Iterator[GroupState]:
        url = GroupStateURI(uri="/api/v1/name/groupstatename/")
        yield from self._retrieve_multi(url, GroupState)


    def group_type_name(self, group_type_name_uri : GroupTypeNameURI) -> Optional[GroupTypeName]:
        return self._retrieve(group_type_name_uri, GroupTypeName)


    def group_type_name_from_slug(self, slug : str) -> Optional[GroupTypeName]:
        return self._retrieve(GroupTypeNameURI(uri=f"/api/v1/name/grouptypename/{slug}/"), GroupTypeName)


    def group_type_names(self) -> Iterator[GroupTypeName]:
        yield from self._retrieve_multi(GroupTypeNameURI(uri="/api/v1/name/grouptypename/"), GroupTypeName)


    # ----------------------------------------------------------------------------------------------------------------------------
    # Datatracker API endpoints returning information about meetings:
    # * https://datatracker.ietf.org/api/v1/meeting/meeting/                        - list of meetings
    # * https://datatracker.ietf.org/api/v1/meeting/meeting/747/                    - information about meeting number 747
    # * https://datatracker.ietf.org/api/v1/meeting/schedule/791/                   - a version of the meeting agenda
    # * https://datatracker.ietf.org/api/v1/meeting/session/25886/                  - a session within a meeting
    # * https://datatracker.ietf.org/api/v1/meeting/session/                        - list of sessions within meetings
    # * https://datatracker.ietf.org/api/v1/meeting/session/?meeting=747            - sessions in meeting number 747
    # * https://datatracker.ietf.org/api/v1/meeting/session/?meeting=747&group=2161 - sessions in meeting number 747 for group 2161
    # * https://datatracker.ietf.org/api/v1/meeting/schedtimesessassignment/59003/  - a schededuled session within a meeting
    # * https://datatracker.ietf.org/api/v1/meeting/schedulingevent/                - sessions being scheduled
    #   https://datatracker.ietf.org/api/v1/meeting/timeslot/9480/                  - a time slot within a meeting (time, duration, location)
    #
    #   https://datatracker.ietf.org/api/v1/meeting/room/537/                       - a room at a meeting
    #   https://datatracker.ietf.org/api/v1/meeting/floorplan/14/                   - floor plan for a meeting venue
    #
    #   https://datatracker.ietf.org/meeting/107/agenda.json
    #   https://datatracker.ietf.org/meeting/interim-2020-hrpc-01/agenda.json
    #
    # * https://datatracker.ietf.org/api/v1/name/sessionstatusname/
    #   https://datatracker.ietf.org/api/v1/name/agendatypename/
    #   https://datatracker.ietf.org/api/v1/name/timeslottypename/
    #   https://datatracker.ietf.org/api/v1/name/roomresourcename/
    # * https://datatracker.ietf.org/api/v1/name/meetingtypename/
    #   https://datatracker.ietf.org/api/v1/name/importantdatename/

    def meeting_attended(self, meeting_attended_uri: MeetingAttendedURI) -> Optional[MeetingAttended]:
        return self._retrieve(meeting_attended_uri, MeetingAttended)


    def meeting_attendance(self, session: Optional[Session]=None, person: Optional[Person]=None) -> Iterator[MeetingAttended]:
        """
        There are four ways to access meeting registration data from the 
        datatracker:

        `meeting_registration()` and `meeting_registration_old()` provide
        information about people who have registered for meetings. These
        two functions provide the same information, although the
        `meeting_registration()` call provides more detail on registrations
        for ANRW and the hackathon.

        `meeting_attendance()` provides information about the people that
        attended a particular session of the meeting. This information is
        not available for older meetings, where attendance was taken using
        hand-written blue sheets.

        `meeting()` provides a count of the number of attendees at the
        meeting as part of the result.  The way in which the number of
        attendees given here is derived has varied over time, and is
        not always a straightforward match with the values returned
        from the other functions.
        """
        url = SessionURI(uri="/api/v1/meeting/attended/")
        if session is not None:
            url.params["session"] = session.id
        if person is not None:
            url.params["person"] = person.id
        yield from self._retrieve_multi(url, MeetingAttended)


    def meeting_session_assignment(self, assignment_uri : SessionAssignmentURI) -> Optional[SessionAssignment]:
        return self._retrieve(assignment_uri, SessionAssignment)


    def meeting_session_assignments(self, schedule : Schedule) -> Iterator[SessionAssignment]:
        """
        The assignment of sessions to timeslots in a meeting schedule.
        """
        url = SessionAssignmentURI(uri="/api/v1/meeting/schedtimesessassignment/")
        url.params["schedule"] = schedule.id
        yield from self._retrieve_multi(url, SessionAssignment)


    def meeting_session_status(self, session: Session) -> Optional[SessionStatusName]:
        sched_events = list(self.meeting_scheduling_events(session=session))
        if len(sched_events) > 0:
            status_name = self.meeting_session_status_name(sched_events[-1].status)
            return status_name
        return None


    def meeting_session_status_name(self, ssn_uri: SessionStatusNameURI) -> Optional[SessionStatusName]:
        return self._retrieve(ssn_uri, SessionStatusName)


    def meeting_session_status_name_from_slug(self, slug: str) -> Optional[SessionStatusName]:
        return self._retrieve(SessionStatusNameURI(uri=f"/api/v1/name/sessionstatusname/{slug}/"), SessionStatusName)


    def meeting_session_status_names(self) -> Iterator[SessionStatusName]:
        yield from self._retrieve_multi(SessionStatusNameURI(uri="/api/v1/name/sessionstatusname/"), SessionStatusName)


    def meeting_session_purpose(self, purpose_uri: SessionPurposeURI) -> Optional[SessionPurpose]:
        return self._retrieve(purpose_uri, SessionPurpose)


    def meeting_session_purposes(self) -> Iterator[SessionPurpose]:
        yield from self._retrieve_multi(SessionPurposeURI(uri="/api/v1/name/sessionpurposename/"), SessionPurpose)


    def meeting_session(self, session_uri : SessionURI) -> Optional[Session]:
        return self._retrieve(session_uri, Session)


    def meeting_sessions(self,
            meeting : Meeting,
            group   : Optional[Group] = None) -> Iterator[Session]:
        url = SessionURI(uri="/api/v1/meeting/session/")
        url.params["meeting"]  = meeting.id
        if group is not None:
            url.params["group"] = group.id
        yield from self._retrieve_multi(url, Session)


    def meeting_timeslot(self, timeslot_uri: TimeslotURI) -> Optional[Timeslot]:
        return self._retrieve(timeslot_uri, Timeslot)


    def meeting_scheduling_event(self, scheduling_event_uri: SchedulingEventURI) -> Optional[SchedulingEvent]:
        return self._retrieve(scheduling_event_uri, SchedulingEvent)


    def meeting_scheduling_events(self,
            by      : Optional[Person]  = None,
            session : Optional[Session] = None) -> Iterator[SchedulingEvent]:
        url = SchedulingEventURI(uri="/api/v1/meeting/schedulingevent/")
        if session is not None:
            url.params["session"] = session.id
        if by is not None:
            url.params["by"] = by.id
        yield from self._retrieve_multi(url, SchedulingEvent)


    def meeting_schedule(self, schedule_uri : ScheduleURI) -> Optional[Schedule]:
        """
        Information about a particular version of the schedule for a meeting.

        Use `meeting_session_assignments()` to find what sessions are scheduled
        in each timeslot of the meeting in this version of the meeting schedule.
        """
        return self._retrieve(schedule_uri, Schedule)


    def meeting(self, meeting_uri : MeetingURI) -> Optional[Meeting]:
        """
        Information about a meeting.

        A meeting comprises a number of `Session`s organised into a `Schedule`.
        Use `meeting_sessions()` to find the sessions that occurred during the
        meeting. Use `meeting_session_assignments()` to find the timeslots when
        those sessions occurred.
        """
        return self._retrieve(meeting_uri, Meeting)


    def meetings(self,
            start_date   : str = "1970-01-01",
            end_date     : str = "2038-01-19",
            meeting_type : Optional[MeetingType] = None) -> Iterator[Meeting]:
        """
        Return information about meetings taking place within a particular date range.

        `meeting()` provides a count of the number of attendees at the
        meeting as part of the result.  The way in which the number of
        attendees given here is derived has varied over time. See also
        the `meeting_registration()`, `meeting_registration_old()`, and
        `meeting_attendance()` functions.
        """
        url = MeetingURI(uri="/api/v1/meeting/meeting/")
        url.params["date__gte"] = start_date
        url.params["date__lte"] = end_date
        if meeting_type is not None:
            url.params["type"] = meeting_type.slug
        yield from self._retrieve_multi(url, Meeting)



    def meeting_type(self, meeting_type_uri: MeetingTypeURI) -> Optional[MeetingType]:
        return self._retrieve(meeting_type_uri, MeetingType)


    def meeting_type_from_slug(self, slug: str) -> Optional[MeetingType]:
        return self._retrieve(MeetingTypeURI(uri=f"/api/v1/name/meetingtypename/{slug}/"), MeetingType)


    def meeting_types(self) -> Iterator[MeetingType]:
        """
        A generator returning the possible meeting types

        Parameters:
           None

        Returns:
            An iterator of MeetingType objects
        """
        yield from self._retrieve_multi(MeetingTypeURI(uri="/api/v1/name/meetingtypename/"), MeetingType)


    def meeting_registration_old(self, meeting_registration_old_uri: MeetingRegistrationOldURI) -> Optional[MeetingRegistrationOld]:
        return self._retrieve(meeting_registration_old_uri, MeetingRegistrationOld)


    def meeting_registrations_old(self,
                affiliation   : Optional[str]             = None,
                attended      : Optional[bool]            = None,
                country_code  : Optional[str]             = None,
                email         : Optional[str]             = None,
                first_name    : Optional[str]             = None,
                last_name     : Optional[str]             = None,
                meeting       : Optional[Meeting]         = None,
                person        : Optional[Person]          = None,
                reg_type      : Optional[str]             = None,
                ticket_type   : Optional[str]             = None) -> Iterator[MeetingRegistrationOld]:
        """
        There are four ways to access meeting registration data from the 
        datatracker:

        `meeting_registration()` and `meeting_registration_old()` provide
        information about people who have registered for meetings. These
        two functions provide the same information, although the
        `meeting_registration()` call provides more detail on registrations
        for ANRW and the hackathon.

        `meeting_attendance()` provides information about the people that
        attended a particular session of the meeting. This information is
        not available for older meetings, where attendance was taken using
        hand-written blue sheets.

        `meeting()` provides a count of the number of attendees at the
        meeting as part of the result.  The way in which the number of
        attendees given here is derived has varied over time, and is
        not always a straightforward match with the values returned
        from the other functions.
        """
        url = MeetingRegistrationOldURI(uri="/api/v1/meeting/registration/")
        if affiliation is not None:
            url.params["affiliation"] = affiliation
        if attended is not None:
            url.params["attended"] = attended
        if country_code is not None:
            url.params["country_code"] = country_code
        if email is not None:
            url.params["email"] = email
        if first_name is not None:
            url.params["first_name"] = first_name
        if last_name is not None:
            url.params["last_name"] = last_name
        if meeting is not None:
            url.params["meeting"] = meeting.id
        if person is not None:
            url.params["person"] = person.id
        if reg_type is not None:
            url.params["reg_type"] = reg_type
        if ticket_type is not None:
            url.params["ticket_type"] = ticket_type
        yield from self._retrieve_multi(url, MeetingRegistrationOld)


    # ----------------------------------------------------------------------------------------------------------------------------
    # Datatracker API endpoints returning information about IPR disclosures:
    #
    #   https://datatracker.ietf.org/api/v1/ipr/iprdocrel/
    # * https://datatracker.ietf.org/api/v1/ipr/iprdisclosurebase/
    #
    # * https://datatracker.ietf.org/api/v1/ipr/genericiprdisclosure/
    # * https://datatracker.ietf.org/api/v1/ipr/holderiprdisclosure/
    # * https://datatracker.ietf.org/api/v1/ipr/thirdpartyiprdisclosure
    #
    #   https://datatracker.ietf.org/api/v1/ipr/nondocspecificiprdisclosure/
    #   https://datatracker.ietf.org/api/v1/ipr/relatedipr/
    #
    #   https://datatracker.ietf.org/api/v1/ipr/iprevent/
    #   https://datatracker.ietf.org/api/v1/ipr/legacymigrationiprevent/
    #
    # * https://datatracker.ietf.org/api/v1/name/iprdisclosurestatename/
    #   https://datatracker.ietf.org/api/v1/name/ipreventtypename/
    # * https://datatracker.ietf.org/api/v1/name/iprlicensetypename/

    def ipr_disclosure_state(self, ipr_disclosure_state_uri: IPRDisclosureStateURI) -> Optional[IPRDisclosureState]:
        return self._retrieve(ipr_disclosure_state_uri, IPRDisclosureState)


    def ipr_disclosure_states(self) -> Iterator[IPRDisclosureState]:
        yield from self._retrieve_multi(IPRDisclosureStateURI(uri="/api/v1/name/iprdisclosurestatename/"), IPRDisclosureState)


    def ipr_disclosure_base(self, ipr_disclosure_base_uri: IPRDisclosureBaseURI) -> Optional[IPRDisclosureBase]:
        return self._retrieve(ipr_disclosure_base_uri, IPRDisclosureBase)


    def ipr_disclosure_bases(self,
            since              : str                             = "1970-01-01T00:00:00",
            until              : str                             = "2038-01-19T03:14:07",
            by                 : Optional[Person]                = None,
            holder_legal_name  : Optional[str]                   = None,
            state              : Optional[IPRDisclosureState]    = None,
            submitter_email    : Optional[str]                   = None,
            submitter_name     : Optional[str]                   = None) -> Iterator[IPRDisclosureBase]:
        url = IPRDisclosureBaseURI(uri="/api/v1/ipr/iprdisclosurebase/")
        url.params["time__gte"] = since
        url.params["time__lt"]  = until
        if by is not None:
            url.params["by"] = by.id
        if holder_legal_name is not None:
            url.params["holder_legal_name"] = holder_legal_name
        if state is not None:
            url.params["state"] = state.slug
        if submitter_email is not None:
            url.params["submitter_email"] = submitter_email
        if submitter_name is not None:
            url.params["submitter_name"] = submitter_name
        yield from self._retrieve_multi(url, IPRDisclosureBase)


    def generic_ipr_disclosure(self, generic_ipr_disclosure_uri: GenericIPRDisclosureURI) -> Optional[GenericIPRDisclosure]:
        return self._retrieve(generic_ipr_disclosure_uri, GenericIPRDisclosure)


    def generic_ipr_disclosures(self,
            since               : str                             = "1970-01-01T00:00:00",
            until               : str                             = "2038-01-19T03:14:07",
            by                  : Optional[Person]                = None,
            holder_legal_name   : Optional[str]                   = None,
            holder_contact_name : Optional[str]                   = None,
            state               : Optional[IPRDisclosureState]    = None,
            submitter_email     : Optional[str]                   = None,
            submitter_name      : Optional[str]                   = None) -> Iterator[GenericIPRDisclosure]:
        url = GenericIPRDisclosureURI(uri="/api/v1/ipr/genericiprdisclosure/")
        url.params["time__gte"] = since
        url.params["time__lt"]  = until
        if by is not None:
            url.params["by"] = by.id
        if holder_legal_name is not None:
            url.params["holder_legal_name"] = holder_legal_name
        if holder_contact_name is not None:
            url.params["holder_contact_name"] = holder_contact_name
        if state is not None:
            url.params["state"] = state.slug
        if submitter_email is not None:
            url.params["submitter_email"] = submitter_email
        if submitter_name is not None:
            url.params["submitter_name"] = submitter_name
        yield from self._retrieve_multi(url, GenericIPRDisclosure)


    def ipr_license_type(self, ipr_license_type_uri: IPRLicenseTypeURI) -> Optional[IPRLicenseType]:
        return self._retrieve(ipr_license_type_uri, IPRLicenseType)


    def ipr_license_types(self) -> Iterator[IPRLicenseType]:
        yield from self._retrieve_multi(IPRLicenseTypeURI(uri="/api/v1/name/iprlicensetypename/"), IPRLicenseType)


    def holder_ipr_disclosure(self, holder_ipr_disclosure_uri: HolderIPRDisclosureURI) -> Optional[HolderIPRDisclosure]:
        return self._retrieve(holder_ipr_disclosure_uri, HolderIPRDisclosure)


    def holder_ipr_disclosures(self,
            since                : str                             = "1970-01-01T00:00:00",
            until                : str                             = "2038-01-19T03:14:07",
            by                   : Optional[Person]                = None,
            holder_legal_name    : Optional[str]                   = None,
            holder_contact_name  : Optional[str]                   = None,
            ietfer_contact_email : Optional[str]                   = None,
            ietfer_name          : Optional[str]                   = None,
            licensing            : Optional[IPRLicenseType]        = None,
            state                : Optional[IPRDisclosureState]    = None,
            submitter_email      : Optional[str]                   = None,
            submitter_name       : Optional[str]                   = None) -> Iterator[HolderIPRDisclosure]:
        url = HolderIPRDisclosureURI(uri="/api/v1/ipr/holderiprdisclosure/")
        url.params["time__gte"] = since
        url.params["time__lt"]  = until
        if by is not None:
            url.params["by"] = by.id
        if holder_legal_name is not None:
            url.params["holder_legal_name"] = holder_legal_name
        if holder_contact_name is not None:
            url.params["holder_contact_name"] = holder_contact_name
        if ietfer_contact_email is not None:
            url.params["ietfer_contact_email"] = ietfer_contact_email
        if ietfer_name is not None:
            url.params["ietfer_name"] = ietfer_name
        if licensing is not None:
            url.params["licensing"] = licensing.slug
        if state is not None:
            url.params["state"] = state.slug
        if submitter_email is not None:
            url.params["submitter_email"] = submitter_email
        if submitter_name is not None:
            url.params["submitter_name"] = submitter_name
        yield from self._retrieve_multi(url, HolderIPRDisclosure)


    def thirdparty_ipr_disclosure(self, thirdparty_ipr_disclosure_uri: ThirdPartyIPRDisclosureURI) -> Optional[ThirdPartyIPRDisclosure]:
        return self._retrieve(thirdparty_ipr_disclosure_uri, ThirdPartyIPRDisclosure)


    def thirdparty_ipr_disclosures(self,
            since                : str                             = "1970-01-01T00:00:00",
            until                : str                             = "2038-01-19T03:14:07",
            by                   : Optional[Person]                = None,
            holder_legal_name    : Optional[str]                   = None,
            ietfer_contact_email : Optional[str]                   = None,
            ietfer_name          : Optional[str]                   = None,
            state                : Optional[IPRDisclosureState]    = None,
            submitter_email      : Optional[str]                   = None,
            submitter_name       : Optional[str]                   = None) -> Iterator[HolderIPRDisclosure]:
        url = ThirdPartyIPRDisclosureURI(uri="/api/v1/ipr/thirdpartyiprdisclosure/")
        url.params["time__gte"] = since
        url.params["time__lt"]  = until
        if by is not None:
            url.params["by"] = by.id
        if holder_legal_name is not None:
            url.params["holder_legal_name"] = holder_legal_name
        if ietfer_contact_email is not None:
            url.params["ietfer_contact_email"] = ietfer_contact_email
        if ietfer_name is not None:
            url.params["ietfer_name"] = ietfer_name
        if state is not None:
            url.params["state"] = state.slug
        if submitter_email is not None:
            url.params["submitter_email"] = submitter_email
        if submitter_name is not None:
            url.params["submitter_name"] = submitter_name
        yield from self._retrieve_multi(url, HolderIPRDisclosure)


    # ----------------------------------------------------------------------------------------------------------------------------
    # Datatracker API endpoints returning information about liaison statements:
    #
    #   https://datatracker.ietf.org/api/v1/liaisons/liaisonstatement/
    #   https://datatracker.ietf.org/api/v1/liaisons/liaisonstatementevent/
    #   https://datatracker.ietf.org/api/v1/liaisons/liaisonstatementgroupcontacts/
    #   https://datatracker.ietf.org/api/v1/liaisons/relatedliaisonstatement/
    #   https://datatracker.ietf.org/api/v1/liaisons/liaisonstatementattachment/
    #
    #   https://datatracker.ietf.org/api/v1/name/liaisonstatementeventtypename/
    #   https://datatracker.ietf.org/api/v1/name/liaisonstatementpurposename/
    #   https://datatracker.ietf.org/api/v1/name/liaisonstatementstate/
    #   https://datatracker.ietf.org/api/v1/name/liaisonstatementtagname/

    # FIXME: implement these


    # ----------------------------------------------------------------------------------------------------------------------------
    # Datatracker API endpoints returning information about reviews:
    #
    # * https://datatracker.ietf.org/api/v1/review/reviewassignment/
    # * https://datatracker.ietf.org/api/v1/review/reviewrequest/
    # * https://datatracker.ietf.org/api/v1/review/reviewwish/
    # * https://datatracker.ietf.org/api/v1/review/reviewteamsettings/
    # * https://datatracker.ietf.org/api/v1/review/nextreviewerinteam/
    # * https://datatracker.ietf.org/api/v1/review/historicalunavailableperiod/
    # * https://datatracker.ietf.org/api/v1/review/historicalreviewrequest/
    # * https://datatracker.ietf.org/api/v1/review/reviewersettings/
    # * https://datatracker.ietf.org/api/v1/review/unavailableperiod/
    # * https://datatracker.ietf.org/api/v1/review/historicalreviewersettings/
    # * https://datatracker.ietf.org/api/v1/review/historicalreviewassignment/
    # * https://datatracker.ietf.org/api/v1/review/reviewsecretarysettings/

    # * https://datatracker.ietf.org/api/v1/name/reviewresultname/
    # * https://datatracker.ietf.org/api/v1/name/reviewassignmentstatename/
    # * https://datatracker.ietf.org/api/v1/name/reviewrequeststatename/
    # * https://datatracker.ietf.org/api/v1/name/reviewtypename/

    def review_assignment_state(self, review_assignment_state_uri: ReviewAssignmentStateURI) -> Optional[ReviewAssignmentState]:
        return self._retrieve(review_assignment_state_uri, ReviewAssignmentState)


    def review_assignment_state_from_slug(self, slug: str) -> Optional[ReviewAssignmentState]:
        return self._retrieve(ReviewAssignmentStateURI(uri=f"/api/v1/name/reviewassignmentstatename/{slug}/"), ReviewAssignmentState)


    def review_assignment_states(self) -> Iterator[ReviewAssignmentState]:
        yield from self._retrieve_multi(ReviewAssignmentStateURI(uri="/api/v1/name/reviewassignmentstatename/"), ReviewAssignmentState)


    def review_result_type(self, review_result_uri: ReviewResultTypeURI) -> Optional[ReviewResultType]:
        return self._retrieve(review_result_uri, ReviewResultType)


    def review_result_type_from_slug(self, slug: str) -> Optional[ReviewResultType]:
        return self._retrieve(ReviewResultTypeURI(uri=f"/api/v1/name/reviewresultname/{slug}/"), ReviewResultType)


    def review_result_types(self) -> Iterator[ReviewResultType]:
        yield from self._retrieve_multi(ReviewResultTypeURI(uri="/api/v1/name/reviewresultname/"), ReviewResultType)


    def review_type(self, review_type_uri: ReviewTypeURI) -> Optional[ReviewType]:
        return self._retrieve(review_type_uri, ReviewType)


    def review_type_from_slug(self, slug: str) -> Optional[ReviewType]:
        return self._retrieve(ReviewTypeURI(uri=f"/api/v1/name/reviewtypename/{slug}/"), ReviewType)


    def review_types(self) -> Iterator[ReviewType]:
        yield from self._retrieve_multi(ReviewTypeURI(uri="/api/v1/name/reviewtypename/"), ReviewType)


    def review_request_state(self, review_request_state_uri: ReviewRequestStateURI) -> Optional[ReviewRequestState]:
        return self._retrieve(review_request_state_uri, ReviewRequestState)


    def review_request_state_from_slug(self, slug: str) -> Optional[ReviewRequestState]:
        return self._retrieve(ReviewRequestStateURI(uri=f"/api/v1/name/reviewrequeststatename/{slug}/"), ReviewRequestState)


    def review_request_states(self) -> Iterator[ReviewRequestState]:
        yield from self._retrieve_multi(ReviewRequestStateURI(uri="/api/v1/name/reviewrequeststatename/"), ReviewRequestState)


    def review_request(self, review_request_uri: ReviewRequestURI) -> Optional[ReviewRequest]:
        return self._retrieve(review_request_uri, ReviewRequest)


    def review_requests(self,
            since         : str                          = "1970-01-01T00:00:00",
            until         : str                          = "2038-01-19T03:14:07",
            doc           : Optional[Document]           = None,
            requested_by  : Optional[Person]             = None,
            state         : Optional[ReviewRequestState] = None,
            team          : Optional[Group]              = None,
            type          : Optional[ReviewType]         = None) -> Iterator[ReviewRequest]:
        url = ReviewRequestURI(uri="/api/v1/review/reviewrequest/")
        url.params["time__gte"] = since
        url.params["time__lt"]  = until
        if doc is not None:
            url.params["doc"] = doc.id
            url.params_alt["doc"] = doc.name
        if requested_by is not None:
            url.params["requested_by"] = requested_by.id
        if state is not None:
            url.params["state"] = state.slug
        if team is not None:
            url.params["team"] = team.id
        if type is not None:
            url.params["type"] = type.slug
        yield from self._retrieve_multi(url, ReviewRequest)


    def review_assignment(self, review_assignment_uri: ReviewAssignmentURI) -> Optional[ReviewAssignment]:
        return self._retrieve(review_assignment_uri, ReviewAssignment)


    def review_assignments(self,
            assigned_since         : str                             = "1970-01-01T00:00:00",
            assigned_until         : str                             = "2038-01-19T03:14:07",
            completed_since        : str                             = "1970-01-01T00:00:00",
            completed_until        : str                             = "2038-01-19T03:14:07",
            result                 : Optional[ReviewResultType]      = None,
            review_request         : Optional[ReviewRequest]         = None,
            reviewer               : Optional[Email]                 = None,
            state                  : Optional[ReviewAssignmentState] = None) -> Iterator[ReviewAssignment]:
        url = ReviewAssignmentURI(uri="/api/v1/review/reviewassignment/")
        url.params["assigned_on__gt"]       = assigned_since
        url.params["assigned_on__lt"]       = assigned_until
        url.params["completed_on__gt"]      = completed_since
        url.params["completed_on__lt"]      = completed_until
        if result is not None:
            url.params["result"] = result.slug
        if review_request is not None:
            url.params["review_request"] = review_request.id
        if reviewer is not None:
            url.params["reviewer"] = reviewer.address
        if state is not None:
            url.params["state"] = state.slug
        yield from self._retrieve_multi(url, ReviewAssignment)


    def review_wish(self, review_wish_uri: ReviewWishURI) -> Optional[ReviewWish]:
        return self._retrieve(review_wish_uri, ReviewWish)


    def review_wishes(self,
            since         : str                          = "1970-01-01T00:00:00",
            until         : str                          = "2038-01-19T03:14:07",
            doc           : Optional[Document]           = None,
            person        : Optional[Person]             = None,
            team          : Optional[Group]              = None) -> Iterator[ReviewWish]:
        url = ReviewWishURI(uri="/api/v1/review/reviewwish/")
        url.params["time__gte"]       = since
        url.params["time__lt"]       = until
        if doc is not None:
            url.params["doc"] = doc.id
            url.params_alt["doc"] = doc.name
        if person is not None:
            url.params["person"] = person.id
        if team is not None:
            url.params["team"] = team.id
        yield from self._retrieve_multi(url, ReviewWish)


    def historical_unavailable_period(self, historical_unavailable_period_uri: HistoricalUnavailablePeriodURI) -> Optional[HistoricalUnavailablePeriod]:
        return self._retrieve(historical_unavailable_period_uri, HistoricalUnavailablePeriod)


    def historical_unavailable_periods(self,
            history_type  : Optional[str]                = None,
            id            : Optional[int]                = None,
            person        : Optional[Person]             = None,
            team          : Optional[Group]              = None) -> Iterator[HistoricalUnavailablePeriod]:
        url = HistoricalUnavailablePeriodURI(uri="/api/v1/review/historicalunavailableperiod/")
        if history_type is not None:
            url.params["history_type"] = history_type
        if id is not None:
            url.params["id"] = id
        if person is not None:
            url.params["person"] = person.id
        if team is not None:
            url.params["team"] = team.id
        yield from self._retrieve_multi(url, HistoricalUnavailablePeriod)


    def historical_review_request(self, historical_review_request_uri: HistoricalReviewRequestURI) -> Optional[HistoricalReviewRequest]:
        return self._retrieve(historical_review_request_uri, HistoricalReviewRequest)


    def historical_review_requests(self,
            since         : str                          = "1970-01-01T00:00:00",
            until         : str                          = "2038-01-19T03:14:07",
            history_since : str                          = "1970-01-01T00:00:00",
            history_until : str                          = "2038-01-19T03:14:07",
            history_type  : Optional[str]                = None,
            id            : Optional[int]                = None,
            doc           : Optional[Document]           = None,
            requested_by  : Optional[Person]             = None,
            state         : Optional[ReviewRequestState] = None,
            team          : Optional[Group]              = None,
            type          : Optional[ReviewType]         = None) -> Iterator[HistoricalReviewRequest]:
        url = HistoricalReviewRequestURI(uri="/api/v1/review/historicalreviewrequest/")
        url.params["time__gte"]         = since
        url.params["time__lt"]         = until
        url.params["history_date__gt"] = history_since
        url.params["history_date__lt"] = history_until
        if doc is not None:
            url.params["doc"] = doc.id
            url.params_alt["doc"] = doc.name
        if requested_by is not None:
            url.params["requested_by"] = requested_by.id
        if state is not None:
            url.params["state"] = state.slug
        if team is not None:
            url.params["team"] = team.id
        if type is not None:
            url.params["type"] = type.slug
        yield from self._retrieve_multi(url, HistoricalReviewRequest)


    def next_reviewer_in_team(self, next_reviewer_in_team_uri: NextReviewerInTeamURI) -> Optional[NextReviewerInTeam]:
        return self._retrieve(next_reviewer_in_team_uri, NextReviewerInTeam)


    def next_reviewers_in_teams(self,
            team          : Optional[Group] = None) -> Iterator[NextReviewerInTeam]:
        url = NextReviewerInTeamURI(uri="/api/v1/review/nextreviewerinteam/")
        if team is not None:
            url.params["team"] = team.id
        yield from self._retrieve_multi(url, NextReviewerInTeam)


    def review_team_settings(self, review_team_settings_uri: ReviewTeamSettingsURI) -> Optional[ReviewTeamSettings]:
        return self._retrieve(review_team_settings_uri, ReviewTeamSettings)


    def review_team_settings_all(self,
            group                    : Optional[Group] = None) -> Iterator[ReviewTeamSettings]:
        url = ReviewTeamSettingsURI(uri="/api/v1/review/reviewteamsettings/")
        if group is not None:
            url.params["group"] = group.id
        yield from self._retrieve_multi(url, ReviewTeamSettings)


    def reviewer_settings(self, reviewer_settings_uri: ReviewerSettingsURI) -> Optional[ReviewerSettings]:
        return self._retrieve(reviewer_settings_uri, ReviewerSettings)


    def reviewer_settings_all(self,
            person        : Optional[Person]             = None,
            team          : Optional[Group]              = None) -> Iterator[ReviewerSettings]:
        url = ReviewerSettingsURI(uri="/api/v1/review/reviewersettings/")
        if person is not None:
            url.params["person"] = person.id
        if team is not None:
            url.params["team"] = team.id
        yield from self._retrieve_multi(url, ReviewerSettings)


    def unavailable_period(self, unavailable_period_uri: UnavailablePeriodURI) -> Optional[UnavailablePeriod]:
        return self._retrieve(unavailable_period_uri, UnavailablePeriod)


    def unavailable_periods(self,
            person        : Optional[Person]             = None,
            team          : Optional[Group]              = None) -> Iterator[UnavailablePeriod]:
        url = UnavailablePeriodURI(uri="/api/v1/review/unavailableperiod/")
        if person is not None:
            url.params["person"] = person.id
        if team is not None:
            url.params["team"] = team.id
        yield from self._retrieve_multi(url, UnavailablePeriod)


    def historical_reviewer_settings(self, historical_reviewer_settings_uri: HistoricalReviewerSettingsURI) -> Optional[HistoricalReviewerSettings]:
        return self._retrieve(historical_reviewer_settings_uri, HistoricalReviewerSettings)


    def historical_reviewer_settings_all(self,
            history_since : str                          = "1970-01-01T00:00:00",
            history_until : str                          = "2038-01-19T03:14:07",
            id            : Optional[int]                = None,
            person        : Optional[Person]             = None,
            team          : Optional[Group]              = None) -> Iterator[HistoricalReviewerSettings]:
        url = HistoricalReviewerSettingsURI(uri="/api/v1/review/historicalreviewersettings/")
        url.params["history_date__gt"]       = history_since
        url.params["history_date__lt"]       = history_until
        if id is not None:
            url.params["id"] = id
        if person is not None:
            url.params["person"] = person.id
        if team is not None:
            url.params["team"] = team.id
        yield from self._retrieve_multi(url, HistoricalReviewerSettings)


    def historical_review_assignment(self, historical_review_assignment_uri: HistoricalReviewAssignmentURI) -> Optional[HistoricalReviewAssignment]:
        return self._retrieve(historical_review_assignment_uri, HistoricalReviewAssignment)


    def historical_review_assignments(self,
            assigned_since         : str                             = "1970-01-01T00:00:00",
            assigned_until         : str                             = "2038-01-19T03:14:07",
            completed_since        : str                             = "1970-01-01T00:00:00",
            completed_until        : str                             = "2038-01-19T03:14:07",
            id                     : Optional[int]                   = None,
            result                 : Optional[ReviewResultType]      = None,
            review_request         : Optional[ReviewRequest]         = None,
            reviewer               : Optional[Email]                 = None,
            state                  : Optional[ReviewAssignmentState] = None) -> Iterator[HistoricalReviewAssignment]:
        url = HistoricalReviewAssignmentURI(uri="/api/v1/review/historicalreviewassignment/")
        url.params["assigned_on__gt"]       = assigned_since
        url.params["assigned_on__lt"]       = assigned_until
        url.params["completed_on__gt"]      = completed_since
        url.params["completed_on__lt"]      = completed_until
        if id is not None:
            url.params["id"] = id
        if result is not None:
            url.params["result"] = result.slug
        if review_request is not None:
            url.params["review_request"] = review_request.id
        if reviewer is not None:
            url.params["reviewer"] = reviewer.address
        if state is not None:
            url.params["state"] = state.slug
        yield from self._retrieve_multi(url, HistoricalReviewAssignment)


    def review_secretary_settings(self, review_secretary_settings_uri: ReviewSecretarySettingsURI) -> Optional[ReviewSecretarySettings]:
        return self._retrieve(review_secretary_settings_uri, ReviewSecretarySettings)


    def review_secretary_settings_all(self,
            person        : Optional[Person]             = None,
            team          : Optional[Group]              = None) -> Iterator[ReviewSecretarySettings]:
        url = ReviewSecretarySettingsURI(uri="/api/v1/review/reviewsecretarysettings/")
        if person is not None:
            url.params["person"] = person.id
        if team is not None:
            url.params["team"] = team.id
        yield from self._retrieve_multi(url, ReviewSecretarySettings)


    # ----------------------------------------------------------------------------------------------------------------------------
    # Datatracker API endpoints returning information about mailing lists:
    #
    #   https://datatracker.ietf.org/api/v1/mailinglists/list/
    #   https://datatracker.ietf.org/api/v1/mailinglists/subscribed/

    # These appear to have been removed in datatracker 12.5.0

    # def email_list(self, email_list_uri: EmailListURI) -> Optional[EmailList]:
    #     return self._retrieve(email_list_uri, EmailList)


    # def email_lists(self, name : Optional[str] = None) -> Iterator[EmailList]:
    #     url = EmailListURI(uri="/api/v1/mailinglists/list/")
    #     if name is not None:
    #         url.params["name"] = name
    #     yield from self._retrieve_multi(url, EmailList)


    # def email_list_subscriptions(self,
    #         email_addr : Optional[str] = None,
    #         email_list : Optional[EmailList] = None) -> Iterator[EmailListSubscriptions]:
    #     url = EmailListSubscriptionsURI(uri="/api/v1/mailinglists/subscribed/")
    #     if email_addr is not None:
    #         url.params["email"] = email_addr
    #     if email_list is not None:
    #         url.params["lists"] = email_list.id
    #     yield from self._retrieve_multi(url, EmailListSubscriptions)


    # ----------------------------------------------------------------------------------------------------------------------------
    # Datatracker API endpoints returning information about names:
    #
    # FIXME: move these into the appropriate place
    #
    #   https://datatracker.ietf.org/api/v1/name/dbtemplatetypename/
    #   https://datatracker.ietf.org/api/v1/name/docrelationshipname/
    #   https://datatracker.ietf.org/api/v1/name/docurltagname/
    #   https://datatracker.ietf.org/api/v1/name/formallanguagename/
    #   https://datatracker.ietf.org/api/v1/name/stdlevelname/
    #   https://datatracker.ietf.org/api/v1/name/groupmilestonestatename/
    #   https://datatracker.ietf.org/api/v1/name/feedbacktypename/
    #   https://datatracker.ietf.org/api/v1/name/topicaudiencename/
    #   https://datatracker.ietf.org/api/v1/name/nomineepositionstatename/
    #   https://datatracker.ietf.org/api/v1/name/constraintname/
    #   https://datatracker.ietf.org/api/v1/name/docremindertypename/
    #   https://datatracker.ietf.org/api/v1/name/intendedstdlevelname/
    #   https://datatracker.ietf.org/api/v1/name/draftsubmissionstatename/
    #   https://datatracker.ietf.org/api/v1/name/rolename/

    # ----------------------------------------------------------------------------------------------------------------------------
    # Datatracker API endpoints returning information about countries:
    # * https://datatracker.ietf.org/api/v1/stats/countryalias/
    # * https://datatracker.ietf.org/api/v1/name/countryname/
    # * https://datatracker.ietf.org/api/v1/name/continentname/

    def continent(self, continent_uri : ContinentURI) -> Optional[Continent]:
        return self._retrieve(continent_uri, Continent)


    def continent_from_slug(self, slug : str) -> Optional[Continent]:
        return self._retrieve(ContinentURI(uri=f"/api/v1/name/continentname/{slug}/"), Continent)


    def continents(self) -> Iterator[Continent]:
        url = ContinentURI(uri="/api/v1/name/continentname/")
        yield from self._retrieve_multi(url, Continent)


    def country(self, country_uri: CountryURI) -> Optional[Country]:
        return self._retrieve(country_uri, Country)


    def country_from_slug(self, slug : str) -> Optional[Country]:
        return self._retrieve(CountryURI(uri=f"/api/v1/name/countryname/{slug}/"), Country)


    def countries(self,
                  continent_slug : Optional[str]  = None,
                  in_eu          : Optional[bool] = None,
                  slug           : Optional[str]  = None,
                  name           : Optional[str]  = None) -> Iterator[Country]:
        url = CountryURI(uri="/api/v1/name/countryname/")
        if continent_slug is not None:
            url.params["continent"] = continent_slug
        if in_eu is not None:
            url.params["in_eu"] = in_eu
        if slug is not None:
            url.params["slug"] = slug
        if name is not None:
            url.params["name"] = name
        yield from self._retrieve_multi(url, Country)


    def country_alias(self, country_alias_uri : CountryAliasURI) -> Optional[CountryAlias]:
        return self._retrieve(country_alias_uri, CountryAlias)


    def country_aliases(self, alias : str) -> Iterator[CountryAlias]:
        url = CountryAliasURI(uri="/api/v1/stats/countryalias/")
        url.params["alias"] = alias
        yield from self._retrieve_multi(url, CountryAlias)


    # ----------------------------------------------------------------------------------------------------------------------------
    # Datatracker API endpoints returning information about statistics:
    #
    #   https://datatracker.ietf.org/api/v1/stats/affiliationalias/
    #   https://datatracker.ietf.org/api/v1/stats/affiliationignoredending/
    #   https://datatracker.ietf.org/api/v1/stats/meetingregistration/

    def meeting_registration(self, meeting_registration_uri: MeetingRegistrationURI) -> Optional[MeetingRegistration]:
        return self._retrieve(meeting_registration_uri, MeetingRegistration)


    def meeting_registrations(self,
                affiliation   : Optional[str]             = None,
                attended      : Optional[bool]            = None,
                country_code  : Optional[str]             = None,
                email         : Optional[str]             = None,
                first_name    : Optional[str]             = None,
                last_name     : Optional[str]             = None,
                meeting       : Optional[Meeting]         = None,
                person        : Optional[Person]          = None,
                reg_type      : Optional[str]             = None,
                ticket_type   : Optional[str]             = None) -> Iterator[MeetingRegistration]:
        """
        There are four ways to access meeting registration data from the 
        datatracker:

        `meeting_registration()` and `meeting_registration_old()` provide
        information about people who have registered for meetings. These
        two functions provide the same information, although the
        `meeting_registration()` call provides more detail on registrations
        for ANRW and the hackathon.

        `meeting_attendance()` provides information about the people that
        attended a particular session of the meeting. This information is
        not available for older meetings, where attendance was taken using
        hand-written blue sheets.

        `meeting()` provides a count of the number of attendees at the
        meeting as part of the result.  The way in which the number of
        attendees given here is derived has varied over time, and is
        not always a straightforward match with the values returned
        from the other functions.
        """
        url = MeetingRegistrationURI(uri="/api/v1/stats/meetingregistration/")
        if affiliation is not None:
            url.params["affiliation"] = affiliation
        if attended is not None:
            url.params["attended"] = attended
        if country_code is not None:
            url.params["country_code"] = country_code
        if email is not None:
            url.params["email"] = email
        if first_name is not None:
            url.params["first_name"] = first_name
        if last_name is not None:
            url.params["last_name"] = last_name
        if meeting is not None:
            url.params["meeting"] = meeting.id
        if person is not None:
            url.params["person"] = person.id
        if reg_type is not None:
            url.params["reg_type"] = reg_type
        if ticket_type is not None:
            url.params["ticket_type"] = ticket_type
        yield from self._retrieve_multi(url, MeetingRegistration)


# =================================================================================================================================
# vim: set tw=0 ai:
